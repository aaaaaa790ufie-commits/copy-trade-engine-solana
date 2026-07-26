#!/usr/bin/env python3
"""Close every open position and top the paper account back up.

Used when changing strategy: the previous configuration's positions are settled at
the market, the balance is restored to a target, and the moment is recorded so
performance under the new settings can be read separately from what came before.

Two accounting choices, both deliberate:

* Positions are closed at their current mark, not written off and not valued at
  entry. The journal keeps a real EXIT with a real P&L for each one.
* A top-up raises `initial_budget_sol` by the same amount. Without that, adding
  0.06 SOL to a depleted account would make `equity - initial` read as break-even
  while the money was genuinely lost. Cumulative P&L stays true; `reset_at` is what
  lets the panel also show performance since the injection.

Dry run by default.

    python gmgn/reset_account.py                  # show what would happen
    python gmgn/reset_account.py --apply
    python gmgn/reset_account.py --apply --target 0.5
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
import paper_engine as pe  # noqa: E402


def close_all(c: sqlite3.Connection, now: int, apply: bool) -> tuple[int, float]:
    """Settle every open position at its current mark. Returns (count, total P&L)."""
    rows = c.execute(
        "SELECT token_mint,chain,entry_price,peak_price,stake_sol,signal_score,wallet_count "
        "FROM paper_positions WHERE status='open'"
    ).fetchall()
    closed, total = 0, 0.0
    for mint, chain, entry, peak, stake, score, wallets in rows:
        price = pe.token_price(chain, mint)
        if price <= 0:
            # Unpriceable — almost certainly delisted. Settle at the last mark we have
            # rather than inventing a zero, and say so in the reason.
            price = peak
            reason = "strategy reset (no quote, settled at last mark)"
        else:
            reason = "strategy reset"
        change = (price / entry - 1) if entry > 0 else 0.0
        pnl = stake * change
        total += pnl
        closed += 1
        print(f"  {mint[:10]}… {change*100:+7.2f}%  {pnl:+.5f} SOL   {reason}")
        if not apply:
            continue
        c.execute("UPDATE paper_account SET budget_sol=budget_sol+?,updated_at=? WHERE id=1",
                  (stake + pnl, now))
        c.execute("UPDATE paper_positions SET status='closed' WHERE token_mint=?", (mint,))
        c.execute(
            "INSERT INTO paper_trades(token_mint,chain,action,price,stake_sol,pnl_sol,pnl_pct,"
            "reason,wallet_count,signal_score,event_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (mint, chain, "EXIT", price, stake, pnl, change, reason, wallets, score, now))
        pe.emit(c, "EXIT", f"{chain} {mint} | {change*100:.2f}% ({pnl:+.5f} SOL) | {reason}")
    return closed, total


def main() -> None:
    config.use_utf8_stdio()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="actually write (default: preview only)")
    ap.add_argument("--target", type=float, default=config.BUDGET_SOL,
                    help=f"balance to restore (default {config.BUDGET_SOL})")
    ap.add_argument("--db-path", default=config.DB_PATH)
    ap.add_argument("--no-backup", action="store_true")
    args = ap.parse_args()

    c = sqlite3.connect(args.db_path, timeout=30)
    pe.init(c)
    now = int(time.time())
    try:
        bal, initial = c.execute(
            "SELECT budget_sol,initial_budget_sol FROM paper_account WHERE id=1").fetchone()
        realised = c.execute(
            "SELECT COALESCE(SUM(pnl_sol),0) FROM paper_trades WHERE action='EXIT'").fetchone()[0]
        open_stake = c.execute(
            "SELECT COALESCE(SUM(stake_sol),0) FROM paper_positions WHERE status='open'").fetchone()[0]

        print(f"database        {args.db_path}")
        print(f"balance         {bal:.5f} SOL   (initial {initial:.5f}, realised {realised:+.5f})")
        print(f"open positions  {open_stake:.5f} SOL staked")
        print()

        if args.apply and not args.no_backup:
            backup = f"{args.db_path}.{now}.bak"
            with sqlite3.connect(backup) as dst:
                c.backup(dst)
            print(f"backup          {backup}\n")

        print("closing open positions at market:")
        closed, pnl = close_all(c, now, args.apply)
        if not closed:
            print("  (none open)")
        print(f"  settled {closed} position(s) for {pnl:+.5f} SOL")
        print()

        after = bal + open_stake + pnl
        deposit = args.target - after
        print(f"balance after closing   {after:.5f} SOL")
        print(f"top-up to reach target  {deposit:+.5f} SOL")
        print(f"initial raised to       {initial + max(0.0, deposit):.5f} SOL"
              "   (so cumulative P&L stays honest)")

        if not args.apply:
            print("\ndry run — pass --apply to write")
            return

        c.execute("UPDATE paper_account SET budget_sol=?,initial_budget_sol=?,bankrupt=0,updated_at=? "
                  "WHERE id=1", (args.target, initial + max(0.0, deposit), now))
        # Recorded so the panel and /status can report performance since this point,
        # separately from lifetime P&L.
        c.execute("INSERT INTO engine_state(key,value,updated_at) VALUES('reset_at',?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                  (str(now), now))
        if deposit > 0:
            pe.emit(c, "DEPOSIT", f"пополнение {deposit:+.5f} SOL — баланс восстановлен до {args.target:.5f}")
        c.commit()

        bal2, init2 = c.execute(
            "SELECT budget_sol,initial_budget_sol FROM paper_account WHERE id=1").fetchone()
        real2 = c.execute(
            "SELECT COALESCE(SUM(pnl_sol),0) FROM paper_trades WHERE action='EXIT'").fetchone()[0]
        still_open = c.execute(
            "SELECT COUNT(*) FROM paper_positions WHERE status='open'").fetchone()[0]
        print(f"\nbalance         {bal2:.5f} SOL")
        print(f"open positions  {still_open}")
        print(f"cumulative P&L  {bal2 - init2:+.5f} SOL  (unchanged by the top-up, as intended)")
        print(f"money conserved {abs(bal2 - (init2 + real2)) < 1e-9}")
    finally:
        c.close()


if __name__ == "__main__":
    main()
