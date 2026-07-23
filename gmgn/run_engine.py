#!/usr/bin/env python3
from __future__ import annotations
import argparse, logging, os, sqlite3, time
from paper_engine import DB, POLL, cycle, init, emit, LOG

def import_old_wallets(c):
    """Import ALL wallets from old Sentinel tables (wallet_scores + candidate_wallets)."""
    now = int(time.time())
    old_sources = [
        ("wallet_scores", "wallet_address"),
        ("candidate_wallets", "address"),
    ]
    total = 0
    for table, col in old_sources:
        try:
            rows = c.execute(f"SELECT DISTINCT {col} FROM {table}").fetchall()
            for (addr,) in rows:
                if not addr:
                    continue
                c.execute(
                    "INSERT OR IGNORE INTO wallet_watch(address,chain,source,last_seen,winrate,updated_at) VALUES(?,?,?,?,?,?)",
                    (addr, "sol", "legacy", 0, 0, now),
                )
                total += 1
        except Exception as e:
            LOG.warning("import from %s: %s", table, e)
    c.commit()
    LOG.info("imported %d wallets from old tables into wallet_watch", total)
    return total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--db-path", default=DB)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    c = sqlite3.connect(args.db_path, timeout=30)
    init(c)
    imported = import_old_wallets(c)
    LOG.info("wallet_watch now has %d wallets", c.execute("SELECT COUNT(*) FROM wallet_watch").fetchone()[0])
    try:
        while True:
            cycle(c)
            if args.once:
                break
            time.sleep(POLL)
    finally:
        c.close()


if __name__ == "__main__":
    main()
