#!/usr/bin/env python3
"""Undo blacklist entries created by the old wallet-eligibility bug.

Until commit a2d7e16, `refresh_wallet_stats` overwrote a wallet's win rate with a
synthetic 0.49 when it had too small a sample or no buys in the sampled window,
purely so `cleanup_wallets` would sweep it up. The sweep then blacklisted it, and
discovery never re-adds a blacklisted address — so dormant wallets with good win
rates were banned permanently.

The two cases are indistinguishable after the fact: both land in the blacklist as
`reason='low_winrate'`, with no record of the win rate that caused it. This script
therefore clears `low_winrate` entries so the engine can re-evaluate those wallets
against the corrected logic. Genuinely bad traders will simply be re-banned on
their next stats refresh; the cost is a slower re-verification, not a wrong entry,
because the engine only ever weights a wallet on a freshly fetched win rate.

Dry run by default — nothing is written unless you pass --apply.

    python gmgn/unban_wallets.py                    # report only
    python gmgn/unban_wallets.py --apply            # clear low_winrate bans
    python gmgn/unban_wallets.py --apply --since 2026-07-20
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402


def parse_since(value: str) -> int:
    """Accept YYYY-MM-DD or a raw unix timestamp, returning a unix timestamp."""
    value = value.strip()
    if value.isdigit():
        return int(value)
    try:
        return int(datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        raise SystemExit(f"--since must be YYYY-MM-DD or a unix timestamp, got {value!r}")


def main() -> None:
    config.use_utf8_stdio()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="actually delete (default: report only)")
    ap.add_argument("--db-path", default=config.DB_PATH)
    ap.add_argument("--reason", default="low_winrate", help="blacklist reason to clear")
    ap.add_argument("--since", help="only clear entries blacklisted on/after this date")
    ap.add_argument("--no-backup", action="store_true", help="skip the .bak copy taken before writing")
    args = ap.parse_args()

    where = "reason=?"
    params: list = [args.reason]
    if args.since:
        where += " AND blacklisted_at>=?"
        params.append(parse_since(args.since))

    c = sqlite3.connect(args.db_path, timeout=30)
    try:
        total = c.execute("SELECT COUNT(*) FROM wallet_blacklist").fetchone()[0]
        matched = c.execute(f"SELECT COUNT(*) FROM wallet_blacklist WHERE {where}", params).fetchone()[0]
        watched = c.execute("SELECT COUNT(*) FROM wallet_watch WHERE active=1").fetchone()[0]
        print(f"database        {args.db_path}")
        print(f"blacklist       {total} total, {matched} match reason={args.reason!r}"
              + (f" since {args.since}" if args.since else ""))
        print(f"watch list      {watched} active")

        if not matched:
            print("\nnothing to clear")
            return
        if not args.apply:
            print(f"\ndry run — pass --apply to clear {matched} entries")
            return

        if not args.no_backup:
            backup = f"{args.db_path}.{int(time.time())}.bak"
            # Use the online backup API: a plain file copy of a live WAL database
            # can capture a torn state.
            with sqlite3.connect(backup) as dst:
                c.backup(dst)
            print(f"\nbackup          {backup}")

        c.execute(f"DELETE FROM wallet_blacklist WHERE {where}", params)
        c.commit()
        remaining = c.execute("SELECT COUNT(*) FROM wallet_blacklist").fetchone()[0]
        print(f"cleared         {matched} entries, {remaining} remain")
        print("\nThe engine will re-evaluate these wallets as it rediscovers them.")
        print("Any that really do trade below 50% will be blacklisted again.")
    finally:
        c.close()


if __name__ == "__main__":
    main()
