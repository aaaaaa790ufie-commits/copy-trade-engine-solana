#!/usr/bin/env python3
from __future__ import annotations
import argparse, logging, os, sqlite3, time
from pathlib import Path
import config
from paper_engine import DB, POLL, cli_available, cycle, init, emit, is_blacklisted, run_forever, valid_address, LOG

SEEDS_PATH = Path(config.get("SEED_WALLETS_SOL", str(Path(__file__).resolve().parent.parent / "data" / "seed_wallets_sol.txt")))


def _admit(c, addr, source, now, rejected):
    """Insert one wallet unless it is malformed or already banned.

    Both callers run on every start. Skipping the blacklist check here means a wallet
    that cleanup_wallets banned comes straight back on the next restart, gets its stats
    fetched again, and is banned again — a churn loop that spends API calls on wallets
    already known to be bad.
    """
    if not valid_address(addr):
        rejected.append(addr)
        return 0
    if is_blacklisted(c, addr, "sol"):
        return 0
    c.execute(
        "INSERT OR IGNORE INTO wallet_watch(address,chain,source,last_seen,winrate,updated_at) VALUES(?,?,?,?,?,?)",
        (addr, "sol", source, 0, 0, now),
    )
    return 1


def import_seed_wallets(c):
    """Load manual seed wallets (data/seed_wallets_sol.txt) into wallet_watch as 'manual_seed'."""
    if not SEEDS_PATH.is_file():
        LOG.info("no seed wallet file at %s", SEEDS_PATH)
        return 0
    now = int(time.time())
    total = 0
    rejected: list[str] = []
    for line in SEEDS_PATH.read_text(encoding="utf-8").splitlines():
        addr = line.strip()
        if not addr or addr.startswith("#"):
            continue
        total += _admit(c, addr, "manual_seed", now, rejected)
    c.commit()
    LOG.info("seeded %d manual wallets from %s", total, SEEDS_PATH)
    if rejected:
        LOG.warning("%d seed lines are not Solana addresses, e.g. %r", len(rejected), rejected[0][:64])
    return total


# Tables from the pre-GMGN Sentinel build. Names are literals, never user input — the
# f-string below would otherwise be an injection site.
LEGACY_SOURCES = (("wallet_scores", "wallet_address"), ("candidate_wallets", "address"))


def import_old_wallets(c):
    """Import wallets from the old Sentinel tables, skipping banned and malformed ones."""
    now = int(time.time())
    total = 0
    rejected: list[str] = []
    for table, col in LEGACY_SOURCES:
        try:
            rows = c.execute(f"SELECT DISTINCT {col} FROM {table}").fetchall()
        except Exception as e:
            LOG.warning("import from %s: %s", table, e)
            continue
        for (addr,) in rows:
            if addr:
                total += _admit(c, str(addr), "legacy", now, rejected)
    c.commit()
    LOG.info("imported %d wallets from old tables into wallet_watch", total)
    if rejected:
        LOG.warning("%d legacy rows are not Solana addresses, e.g. %r", len(rejected), rejected[0][:64])
    return total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--db-path", default=DB)
    args = ap.parse_args()
    config.use_utf8_stdio()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Checked before anything else, because the failure is otherwise invisible: the
    # heartbeat still ticks, so /status and the panel both report LIVE, while every
    # poll logs a bare "[WinError 2]" and nothing ever happens. Saying so once and
    # exiting is far more use than running indefinitely in that state.
    if not cli_available():
        raise SystemExit(
            "gmgn-cli was not found — the engine cannot read anything without it.\n"
            "  npm install -g gmgn-cli\n"
            "Then check it resolves: gmgn-cli --version"
        )
    c = sqlite3.connect(args.db_path, timeout=30)
    init(c)
    import_seed_wallets(c)
    imported = import_old_wallets(c)
    LOG.info("wallet_watch now has %d wallets", c.execute("SELECT COUNT(*) FROM wallet_watch").fetchone()[0])
    try:
        run_forever(c, once=args.once)
    finally:
        c.close()


if __name__ == "__main__":
    main()
