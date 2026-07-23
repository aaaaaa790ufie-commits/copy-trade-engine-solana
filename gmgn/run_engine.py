#!/usr/bin/env python3
from __future__ import annotations
import argparse, logging, os, sqlite3, time
from pathlib import Path
from paper_engine import DB, POLL, cycle, init, emit, is_blacklisted, LOG

SEEDS_PATH = Path(os.getenv("SEED_WALLETS_SOL", str(Path(__file__).resolve().parent.parent / "data" / "seed_wallets_sol.txt")))


def import_seed_wallets(c):
    """Load manual seed wallets (data/seed_wallets_sol.txt) into wallet_watch as 'manual_seed'."""
    if not SEEDS_PATH.is_file():
        LOG.info("no seed wallet file at %s", SEEDS_PATH)
        return 0
    now = int(time.time())
    total = 0
    for line in SEEDS_PATH.read_text(encoding="utf-8").splitlines():
        addr = line.strip()
        if not addr or addr.startswith("#") or is_blacklisted(c, addr, "sol"):
            continue
        c.execute(
            "INSERT OR IGNORE INTO wallet_watch(address,chain,source,last_seen,winrate,updated_at) VALUES(?,?,?,?,?,?)",
            (addr, "sol", "manual_seed", 0, 0, now),
        )
        total += 1
    c.commit()
    LOG.info("seeded %d manual wallets from %s", total, SEEDS_PATH)
    return total


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
    import_seed_wallets(c)
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
