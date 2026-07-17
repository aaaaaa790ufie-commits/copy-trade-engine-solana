#!/usr/bin/env python3
"""Sentinel — Discovery Module Entry Point.

Usage:
    python discovery/run_discovery.py [--db-path sentinel.db] [--max-tokens 30]

Runs the full discovery pipeline:
1. Fetches trending/top-gainer tokens from DexScreener
2. Identifies early buyer wallets for each token
3. Cross-references wallets across tokens
4. Loads seed list (if exists)
5. Writes results to SQLite
"""

import argparse
import logging
import os
from typing import Any
import sys
from datetime import datetime, timezone

# Add parent dir to path for shared modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from discovery.dex_screener import DexScreenerClient
from discovery.early_buyer import RpcClient, find_early_buyers, cross_reference_wallets
from discovery.db import (
    init_db,
    get_connection,
    insert_candidate_wallets,
    insert_discovered_token,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("discovery")


def extract_token_address(token: dict[str, Any]) -> str | None:
    """Extract token address from DexScreener response (handles both formats)."""
    addr = token.get("tokenAddress") or token.get("address")
    # From pairs response: baseToken.address
    if not addr and "baseToken" in token:
        addr = token["baseToken"].get("address")
    return addr


def extract_token_symbol(token: dict[str, Any]) -> str:
    """Extract token symbol."""
    sym = token.get("symbol") or ""
    if not sym and "baseToken" in token:
        sym = token["baseToken"].get("symbol", "")
    return sym


def load_seed_list(path: str | None) -> list[dict[str, Any]]:
    """Load manually curated wallet addresses from a seed file."""
    if not path or not os.path.exists(path):
        logger.info("No seed list found at %s — skipping", path)
        return []

    wallets = []
    with open(path) as f:
        for line in f:
            addr = line.strip()
            if addr and not addr.startswith("#") and len(addr) == 44:
                wallets.append({
                    "address": addr,
                    "token_count": 0,
                    "first_seen_slot": 0,
                    "avg_first_seen_slot": 0.0,
                    "metadata": '{"source": "manual_seed"}',
                })
    logger.info("Loaded %d wallets from seed list", len(wallets))
    return wallets


def main() -> None:
    parser = argparse.ArgumentParser(description="Sentinel Discovery Module")
    parser.add_argument("--db-path", default="sentinel.db", help="Path to SQLite DB")
    parser.add_argument(
        "--max-tokens", type=int, default=15,
        help="Max tokens to analyse for early buyers"
    )
    parser.add_argument(
        "--max-wallets-per-token", type=int, default=10,
        help="Max early-buyer wallets to extract per token"
    )
    parser.add_argument(
        "--dex-req-per-min", type=int, default=60,
        help="DexScreener rate limit"
    )
    parser.add_argument(
        "--seed-path", default="discovery/seed_wallets.txt",
        help="Path to manual seed wallet list"
    )
    parser.add_argument(
        "--rpc-endpoint", default=None,
        help="Solana RPC endpoint (default: public mainnet-beta)"
    )
    parser.add_argument(
        "--min-cross-ref", type=int, default=2,
        help="Min token overlap for cross-reference"
    )
    args = parser.parse_args()

    # ── Init DB ───────────────────────────────────────────────────
    init_db()
    conn = get_connection(args.db_path)
    logger.info("Discovery run starting at %s", datetime.now(timezone.utc).isoformat())

    # ── Step 1: Get interesting tokens from DexScreener ──────────
    dex = DexScreenerClient(req_per_min=args.dex_req_per_min)

    # Get latest token profiles (new tokens) + top gainers
    profiles = dex.get_latest_token_profiles(limit=30)
    gainers = dex.get_top_gainers(limit=15)

    logger.info("Profiles: %d recent Solana tokens", len(profiles))
    logger.info("Gainers: %d top gaining pairs", len(gainers))

    # Merge and deduplicate by token address
    seen_tokens: set[str] = set()
    tokens_to_scan: list[dict[str, Any]] = []

    for t in gainers:
        addr = extract_token_address(t)
        if addr and addr not in seen_tokens:
            seen_tokens.add(addr)
            tokens_to_scan.append({
                "address": addr,
                "symbol": extract_token_symbol(t),
                "source": "dex_top_gainer",
            })

    for t in profiles:
        addr = extract_token_address(t)
        if addr and addr not in seen_tokens:
            seen_tokens.add(addr)
            tokens_to_scan.append({
                "address": addr,
                "symbol": extract_token_symbol(t),
                "source": "dex_new_token",
            })

    logger.info(
        "Unique tokens to scan: %d (limit: %d)",
        len(tokens_to_scan), args.max_tokens
    )

    # ── Step 2: Early-buyer reconstruction ────────────────────────
    rpc = RpcClient(endpoint=args.rpc_endpoint)
    all_token_buyers: dict[str, list[dict[str, Any]]] = {}

    for idx, token in enumerate(tokens_to_scan[:args.max_tokens]):
        token_addr = token["address"]
        token_symbol = token.get("symbol", "?")
        token_source = token.get("source", "dex")

        logger.info(
            "[%d/%d] Analysing %s (%s...) source=%s",
            idx + 1, min(len(tokens_to_scan), args.max_tokens),
            token_symbol, token_addr[:12], token_source
        )

        # Store token in DB
        insert_discovered_token(
            {"address": token_addr, "symbol": token_symbol},
            source=token_source,
            conn=conn,
        )

        # Find early buyers
        buyers = find_early_buyers(
            token_mint=token_addr,
            max_wallets=args.max_wallets_per_token,
            rpc=rpc,
        )
        if buyers:
            all_token_buyers[token_addr] = buyers
            insert_candidate_wallets(buyers, source="early_buyer", conn=conn)

    # ── Step 3: Cross-reference wallets ───────────────────────────
    cross_ref = cross_reference_wallets(
        all_token_buyers,
        min_tokens=args.min_cross_ref,
    )
    logger.info(
        "Cross-reference: %d wallets appear across >= %d tokens",
        len(cross_ref), args.min_cross_ref
    )

    if cross_ref:
        insert_candidate_wallets(cross_ref, source="dex_cross_ref", conn=conn)

    # ── Step 4: Load seed list ────────────────────────────────────
    seed_wallets = load_seed_list(args.seed_path)
    if seed_wallets:
        insert_candidate_wallets(seed_wallets, source="seed_list", conn=conn)

    # ── Summary ───────────────────────────────────────────────────
    db_wallets = conn.execute(
        "SELECT COUNT(*) as cnt FROM candidate_wallets"
    ).fetchone()["cnt"]
    db_tokens = conn.execute(
        "SELECT COUNT(*) as cnt FROM discovered_tokens"
    ).fetchone()["cnt"]

    logger.info("=" * 50)
    logger.info("Discovery run complete!")
    logger.info("  Tokens discovered:  %d", db_tokens)
    logger.info("  Candidate wallets:  %d", db_wallets)
    logger.info("  Cross-ref wallets:  %d", len(cross_ref))
    logger.info("  Seed wallets:       %d", len(seed_wallets))
    logger.info("=" * 50)

    conn.close()


if __name__ == "__main__":
    main()
