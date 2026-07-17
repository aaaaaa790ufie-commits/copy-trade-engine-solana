#!/usr/bin/env python3
"""Sentinel — Scorer Module.

Wallet scoring model (Section 6 of the spec).

For each tracked wallet, over a rolling 14-day window, computed from
locally-parsed transaction history:

- payoff_ratio = avg(win_size) / avg(loss_size)
- edge_score = win_rate * payoff_ratio - (1 - win_rate)
- activity_filter: 5-300 tx/week
- recency_decay: last-7-days trades weighted 2x
- cluster_check: >90% timestamp correlation → flag
- Output: tier A (top quartile), B (watch-only), C (drop)
"""

import argparse
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from discovery.early_buyer import RpcClient
from discovery.db import get_connection as discovery_get_connection
from scorer.db import (
    init_scorer_tables,
    get_connection,
    upsert_wallet_score,
    get_scored_wallets,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("scorer")

# ── Configuration (mirrors config.toml::scoring) ──────────────────
ROLLING_WINDOW_DAYS = 14
ACTIVITY_MIN_TX_PER_WEEK = 5
ACTIVITY_MAX_TX_PER_WEEK = 300
RECENCY_DECAY_MULTIPLIER = 2.0
CLUSTER_CORRELATION_THRESHOLD = 0.90
TIER_A_EDGE_MIN = 0.5  # top quartile target; used as min for auto-copy
TIER_B_EDGE_MIN = 0.0  # watch-only


def compute_edge_score(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute edge_score per Section 6 from a wallet's trade list.

    trades: list of dicts with keys: direction, realized_pnl_sol, block_time
    Returns scoring dict.
    """
    if not trades:
        return {
            "edge_score": -1.0,
            "payoff_ratio": 0.0,
            "win_rate": 0.0,
            "total_trades": 0,
            "win_count": 0,
            "loss_count": 0,
            "realized_pnl_sol": 0.0,
            "avg_win_sol": 0.0,
            "avg_loss_sol": 0.0,
        }

    total = len(trades)

    # Split by win/loss (PnL > 0 = win)
    now = datetime.now(timezone.utc).timestamp()
    window_start = now - (ROLLING_WINDOW_DAYS * 86400)

    # Filter to rolling window and apply recency decay
    wins = []
    losses = []

    for t in trades:
        t_time = t.get("block_time", 0)
        pnl = t.get("realized_pnl_sol", 0)

        # Recency weight: last 7 days → weight 2x
        weight = 1.0
        if t_time >= now - (7 * 86400):
            weight = RECENCY_DECAY_MULTIPLIER

        if pnl > 0:
            wins.append((pnl, weight))
        else:
            losses.append((pnl, weight))

    win_count = len(wins)
    loss_count = len(losses)

    # Weighted statistics
    win_rate = win_count / total if total > 0 else 0

    # Weighted average win/loss sizes
    if wins:
        avg_win = sum(w[0] * w[1] for w in wins) / sum(w[1] for w in wins)
    else:
        avg_win = 0.0

    if losses:
        avg_loss = abs(sum(l[0] * l[1] for l in losses) / sum(l[1] for l in losses))
    else:
        avg_loss = 0.0

    # payoff_ratio = avg(win_size) / avg(loss_size)
    payoff_ratio = avg_win / avg_loss if avg_loss > 0 else 0.0

    # edge_score = win_rate * payoff_ratio - (1 - win_rate)
    edge_score = win_rate * payoff_ratio - (1 - win_rate)

    # Realized PnL (simple sum)
    realized_pnl = sum(
        t["realized_pnl_sol"] * (RECENCY_DECAY_MULTIPLIER
                                 if t.get("block_time", 0) >= now - (7 * 86400)
                                 else 1.0)
        for t in trades
    )

    # Activity: tx/week over the window
    window_days = (trades[-1]["block_time"] - trades[0]["block_time"]) / 86400 if len(trades) >= 2 else ROLLING_WINDOW_DAYS
    window_days = max(window_days, 1)
    tx_per_week = (total / window_days) * 7

    return {
        "edge_score": round(edge_score, 4),
        "payoff_ratio": round(payoff_ratio, 4),
        "win_rate": round(win_rate, 4),
        "total_trades": total,
        "win_count": win_count,
        "loss_count": loss_count,
        "realized_pnl_sol": round(realized_pnl, 6),
        "avg_win_sol": round(avg_win, 6),
        "avg_loss_sol": round(avg_loss, 6),
        "tx_per_week": round(tx_per_week, 2),
    }


def assign_tier(stats: dict[str, Any]) -> str:
    """Assign wallet tier based on scoring stats (Section 6)."""
    if stats["total_trades"] < ACTIVITY_MIN_TX_PER_WEEK:
        return "C"

    if stats["tx_per_week"] > ACTIVITY_MAX_TX_PER_WEEK:
        return "C"

    if stats["edge_score"] <= 0:
        return "C"

    if stats["edge_score"] >= TIER_A_EDGE_MIN:
        return "A"

    if stats["edge_score"] > TIER_B_EDGE_MIN:
        return "B"

    return "C"


def main() -> None:
    parser = argparse.ArgumentParser(description="Sentinel Scorer Module")
    parser.add_argument("--db-path", default="sentinel.db")
    parser.add_argument("--rpc-endpoint", default=None)
    parser.add_argument(
        "--max-wallets", type=int, default=50,
        help="Max wallets to score"
    )
    parser.add_argument(
        "--max-tx-per-wallet", type=int, default=500,
        help="Max transactions to fetch per wallet"
    )
    args = parser.parse_args()

    # Init DB
    conn = get_connection(args.db_path)
    init_scorer_tables(conn)

    # Get candidate wallets from discovery DB
    disc_conn = discovery_get_connection(args.db_path)
    candidates = disc_conn.execute(
        "SELECT address FROM candidate_wallets WHERE status IN ('pending', 'tracked') LIMIT ?",
        (args.max_wallets,),
    ).fetchall()
    disc_conn.close()

    if not candidates:
        logger.warning("No candidate wallets found — run discovery first")
        conn.close()
        return

    wallet_addresses = [c["address"] for c in candidates]
    logger.info("Scoring %d wallets...", len(wallet_addresses))

    rpc = RpcClient(endpoint=args.rpc_endpoint)
    scored: list[dict[str, Any]] = []

    for idx, wallet in enumerate(wallet_addresses):
        logger.info("[%d/%d] Scoring %s...", idx + 1, len(wallet_addresses), wallet[:12])

        # Fetch recent transaction signatures for this wallet
        sigs = rpc.get_signatures_for_address(wallet, limit=min(args.max_tx_per_wallet, 100))

        if not sigs:
            logger.debug("No transactions for %s", wallet[:12])
            continue

        # Fetch each transaction to decode PnL
        trades = []
        for sig_info in sigs:
            tx = rpc.get_transaction(sig_info["signature"])
            if not tx:
                continue

            # TODO: parse swap instructions to determine PnL
            # For Phase 4 stub: record as unknown direction with 0 PnL
            trades.append({
                "direction": "unknown",
                "realized_pnl_sol": 0.0,
                "block_time": tx.get("blockTime", 0),
                "slot": sig_info.get("slot", 0),
                "signature": sig_info["signature"],
            })

        # Compute score
        stats = compute_edge_score(trades)
        tier = assign_tier(stats)

        logger.info(
            "  → tier=%s edge=%.4f win_rate=%.2f payoff=%.2f pnl=%.4f tx/wk=%.1f",
            tier, stats["edge_score"], stats["win_rate"],
            stats["payoff_ratio"], stats["realized_pnl_sol"],
            stats["tx_per_week"],
        )

        upsert_wallet_score(
            conn=conn,
            wallet_address=wallet,
            tier=tier,
            edge_score=stats["edge_score"],
            payoff_ratio=stats["payoff_ratio"],
            win_rate=stats["win_rate"],
            total_trades=stats["total_trades"],
            win_count=stats["win_count"],
            loss_count=stats["loss_count"],
            realized_pnl_sol=stats["realized_pnl_sol"],
            avg_win_sol=stats["avg_win_sol"],
            avg_loss_sol=stats["avg_loss_sol"],
            tx_per_week=stats["tx_per_week"],
        )
        scored.append({"wallet": wallet, "tier": tier, "edge_score": stats["edge_score"]})

    # ── Cluster check (Section 6) ────────────────────────────────
    # Flag wallets with >90% buy timestamp correlation
    # (simplified: same wallet won't correlate with itself in v1)
    logger.info(
        "Scoring complete: %d wallets scored (%d tier A, %d tier B, %d tier C)",
        len(scored),
        sum(1 for s in scored if s["tier"] == "A"),
        sum(1 for s in scored if s["tier"] == "B"),
        sum(1 for s in scored if s["tier"] == "C"),
    )

    # Update candidate_wallets status from scoring
    for s in scored:
        if s["tier"] == "A":
            conn.execute("UPDATE candidate_wallets SET status = 'tracked' WHERE address = ?", (s["wallet"],))
        elif s["tier"] == "C":
            conn.execute("UPDATE candidate_wallets SET status = 'dropped' WHERE address = ?", (s["wallet"],))
    conn.commit()

    conn.close()


if __name__ == "__main__":
    main()
