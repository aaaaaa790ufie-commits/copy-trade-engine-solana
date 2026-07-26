#!/usr/bin/env python3
"""GMGN-backed signal producer for Sentinel.

Replaces custom wallet discovery and raw-RPC scoring with two official GMGN
OpenAPI reads through gmgn-cli:
  1. track smartmoney: recent Smart Money buys/sells
  2. portfolio stats: 30d win rate and realized performance

A buy signal is written only when at least N distinct qualifying wallets have
the same token as their latest action inside the cluster window. The Rust
engine consumes these signals from SQLite. This module never submits trades.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
from paper_engine import _find_gmgn as pe_find_gmgn  # noqa: E402
from paper_engine import _is_winrate_key, gmgn_cli, valid_address  # noqa: E402

LOG = logging.getLogger("gmgn-monitor")
# Read through config.py so the repo-local .env applies here too, rather than requiring
# these to be exported into the shell.
DB_PATH = config.DB_PATH
POLL_SECONDS = config.POLL_SECONDS
STATS_TTL_SECONDS = config.get_int("GMGN_STATS_TTL_SECONDS", 900)
WINDOW_SECONDS = config.CLUSTER_WINDOW
MIN_WALLETS = config.get_int("GMGN_MIN_CLUSTER_WALLETS", 3)
MIN_WINRATE = config.get_float("GMGN_MIN_WINRATE", 0.70)
MIN_BUYS_30D = config.get_int("GMGN_MIN_BUYS_30D", 10)
MIN_REALIZED_PROFIT = config.get_float("GMGN_MIN_REALIZED_PROFIT_USD", 0.0)
FEED_LIMIT = config.FEED_LIMIT

_stats_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def run_cli(args: list[str]) -> Any:
    """Delegates to the engine's shared gmgn_cli.

    The local copy this replaced invoked a bare "gmgn-cli", which CreateProcess cannot
    resolve on Windows (it needs the .cmd shim), decoded output with the locale codec so
    any emoji in a token name raised UnicodeDecodeError, and passed no env, so it used
    the machine-wide credentials rather than the project's .env.
    """
    try:
        return gmgn_cli(args)
    except FileNotFoundError as exc:
        raise RuntimeError("gmgn-cli is not installed: npm install -g gmgn-cli") from exc


def ensure_configured() -> None:
    try:
        proc = subprocess.run(
            [pe_find_gmgn(), "config", "--check"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=20, env=config.gmgn_env(),
        )
    except FileNotFoundError as exc:
        raise RuntimeError("gmgn-cli is not installed: npm install -g gmgn-cli") from exc
    if proc.returncode != 0:
        raise RuntimeError(
            "GMGN API key is not configured. Run `gmgn-cli config --apply <key>`, "
            "then `python gmgn/config.py --import-gmgn` to copy it into the project .env"
        )


def unwrap(value: Any) -> Any:
    while isinstance(value, dict) and "data" in value and len(value) <= 4:
        value = value["data"]
    return value


def as_list(value: Any) -> list[dict[str, Any]]:
    value = unwrap(value)
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        for key in ("list", "items", "wallets", "result"):
            if isinstance(value.get(key), list):
                return [item for item in value[key] if isinstance(item, dict)]
        if any(key in value for key in ("address", "wallet_address", "winrate")):
            return [value]
        return [item for item in value.values() if isinstance(item, dict)]
    return []


def number(obj: dict[str, Any], *keys: str, default: float = 0.0) -> float:
    for key in keys:
        value: Any = obj
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                value = None
                break
            value = value[part]
        if value is not None:
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                continue  # try the next key rather than give up on the whole lookup
            # Shared with the engine: a plain `"winrate" in key` test is False for
            # "win_rate", so a percentage under that spelling was never scaled. 75 stayed
            # 75.0, which clears MIN_WINRATE and every other gate that expects a ratio.
            if _is_winrate_key(key):
                if parsed > 1:
                    parsed /= 100.0
                if not 0 <= parsed <= 1:
                    LOG.warning("ignoring out-of-range winrate %r from %s", value, key)
                    continue
            return parsed
    return default


def wallet_address(row: dict[str, Any]) -> str:
    """First well-formed address among the field names GMGN uses, else "".

    Validated for the same reason paper_engine and mass_discovery validate: the result
    is written to SQLite and read back by other components. This copy was missed when
    the check was added in Pass 2 and accepted `<img src=x onerror=...>` verbatim.
    """
    for key in ("wallet_address", "address", "maker", "wallet"):
        value = row.get(key)
        if value is not None and valid_address(str(value)):
            return str(value)
    return ""


def fetch_stats(wallets: list[str]) -> dict[str, dict[str, Any]]:
    now = time.time()
    result: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    for wallet in wallets:
        cached = _stats_cache.get(wallet)
        if cached and now - cached[0] < STATS_TTL_SECONDS:
            result[wallet] = cached[1]
        else:
            missing.append(wallet)

    for start in range(0, len(missing), 10):
        batch = missing[start : start + 10]
        payload = run_cli(
            ["portfolio", "stats", "--chain", "sol", "--wallet", *batch, "--period", "30d"]
        )
        rows = as_list(payload)
        by_address = {wallet_address(row): row for row in rows if wallet_address(row)}
        if len(batch) == 1 and len(rows) == 1 and batch[0] not in by_address:
            by_address[batch[0]] = rows[0]
        for wallet in batch:
            row = by_address.get(wallet, {})
            _stats_cache[wallet] = (now, row)
            result[wallet] = row
        time.sleep(0.25)
    return result


def qualifies(stats: dict[str, Any]) -> bool:
    winrate = number(stats, "winrate", "win_rate", "stats_30d.winrate")
    buys = number(stats, "buy_count", "buy_count_30d", "stats_30d.buy_count")
    profit = number(
        stats, "realized_profit", "realized_profit_30d", "stats_30d.realized_profit"
    )
    return winrate >= MIN_WINRATE and buys >= MIN_BUYS_30D and profit > MIN_REALIZED_PROFIT


def venue_of(trade: dict[str, Any]) -> str:
    token = trade.get("base_token") if isinstance(trade.get("base_token"), dict) else {}
    raw = " ".join(
        str(value).lower()
        for value in (
            trade.get("launchpad"),
            trade.get("launchpad_platform"),
            trade.get("migrated_pool_exchange"),
            token.get("launchpad"),
        )
        if value
    )
    if "pump_amm" in raw or "pumpswap" in raw:
        return "PumpSwap"
    if "pump" in raw:
        return "PumpFun"
    if "ray" in raw:
        return "RaydiumAmmV4"
    return "GMGN"


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS gmgn_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_key TEXT NOT NULL UNIQUE,
            token_mint TEXT NOT NULL,
            source_wallet TEXT NOT NULL,
            venue TEXT NOT NULL,
            amount_usd REAL DEFAULT 0,
            price_usd REAL DEFAULT 0,
            signal_timestamp INTEGER NOT NULL,
            wallet_count INTEGER NOT NULL,
            avg_winrate REAL NOT NULL,
            makers_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            consumed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_gmgn_signals_status ON gmgn_signals(status);
        CREATE TABLE IF NOT EXISTS wallet_scores (
            row_id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet_address TEXT NOT NULL UNIQUE,
            tier TEXT NOT NULL DEFAULT 'C',
            edge_score REAL NOT NULL DEFAULT 0.0
        );
        """
    )
    conn.commit()


def upsert_score(conn: sqlite3.Connection, wallet: str, tier: str, winrate: float) -> None:
    conn.execute(
        """INSERT INTO wallet_scores (wallet_address, tier, edge_score)
           VALUES (?, ?, ?)
           ON CONFLICT(wallet_address) DO UPDATE SET tier=excluded.tier, edge_score=excluded.edge_score""",
        (wallet, tier, winrate),
    )


def produce_signals(conn: sqlite3.Connection) -> int:
    payload = run_cli(["track", "smartmoney", "--chain", "sol", "--limit", str(FEED_LIMIT)])
    trades = as_list(payload)
    cutoff = int(time.time()) - WINDOW_SECONDS
    recent = [
        trade
        for trade in trades
        if int(number(trade, "timestamp")) >= cutoff
        and trade.get("maker")
        and trade.get("base_address")
    ]
    makers = sorted({str(trade["maker"]) for trade in recent})
    stats_by_wallet = fetch_stats(makers)
    good = {wallet for wallet, stats in stats_by_wallet.items() if qualifies(stats)}

    for wallet, stats in stats_by_wallet.items():
        upsert_score(
            conn,
            wallet,
            "A" if wallet in good else "C",
            number(stats, "winrate", "win_rate", "stats_30d.winrate"),
        )

    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for trade in recent:
        wallet = str(trade["maker"])
        if wallet not in good:
            continue
        key = (str(trade["base_address"]), wallet)
        if key not in latest or number(trade, "timestamp") > number(latest[key], "timestamp"):
            latest[key] = trade

    buys_by_token: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for (token, _wallet), trade in latest.items():
        if str(trade.get("side", "")).lower() == "buy":
            buys_by_token[token].append(trade)

    inserted = 0
    for token, buys in buys_by_token.items():
        distinct = {str(trade["maker"]): trade for trade in buys}
        if len(distinct) < MIN_WALLETS:
            continue
        selected = sorted(distinct.values(), key=lambda row: number(row, "timestamp"), reverse=True)
        newest = int(number(selected[0], "timestamp"))
        makers_for_signal = sorted(distinct)
        winrates = [
            number(stats_by_wallet[wallet], "winrate", "win_rate", "stats_30d.winrate")
            for wallet in makers_for_signal
        ]
        avg_winrate = sum(winrates) / len(winrates)
        representative = makers_for_signal[0]
        signal_key = f"{token}:{newest // WINDOW_SECONDS}"
        cursor = conn.execute(
            """INSERT OR IGNORE INTO gmgn_signals
               (signal_key, token_mint, source_wallet, venue, amount_usd, price_usd,
                signal_timestamp, wallet_count, avg_winrate, makers_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                signal_key,
                token,
                representative,
                venue_of(selected[0]),
                sum(number(row, "amount_usd") for row in selected),
                number(selected[0], "price_usd"),
                newest,
                len(makers_for_signal),
                avg_winrate,
                json.dumps(makers_for_signal),
            ),
        )
        if cursor.rowcount:
            inserted += 1
            LOG.warning(
                "SIGNAL token=%s wallets=%d avg_winrate=%.1f%%",
                token,
                len(makers_for_signal),
                avg_winrate * 100,
            )
    conn.commit()
    LOG.info("feed=%d recent=%d qualifying_wallets=%d new_signals=%d", len(trades), len(recent), len(good), inserted)
    return inserted


def self_test() -> None:
    assert as_list({"data": {"list": [{"maker": "x"}]}})[0]["maker"] == "x"
    assert number({"winrate": 72}, "winrate") == 0.72
    assert number({"winrate": 0.72}, "winrate") == 0.72
    assert venue_of({"base_token": {"launchpad": "pump"}}) == "PumpFun"
    print("self-test passed")


def main() -> int:
    parser = argparse.ArgumentParser(description="GMGN Smart Money cluster monitor")
    parser.add_argument("--db-path", default=DB_PATH)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return 0

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ensure_configured()
    conn = sqlite3.connect(args.db_path, timeout=30)
    init_db(conn)
    try:
        while True:
            try:
                produce_signals(conn)
            except Exception as exc:
                LOG.error("poll failed: %s", exc)
            if args.once:
                break
            time.sleep(POLL_SECONDS)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
