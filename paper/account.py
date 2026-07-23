from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Callable


@dataclass
class Position:
    id: int
    token: str
    chain: str
    entry_price: float
    current_price: float
    amount_sol: float
    peak_price: float
    opened_at: int
    strength: float
    wallet_count: int


class PaperAccount:
    def __init__(self, conn: sqlite3.Connection, budget_sol: float = 0.1, entry_sol: float = 0.025):
        self.conn = conn
        self.entry_sol = entry_sol
        self._init_db(budget_sol)

    def _init_db(self, budget: float) -> None:
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS paper_account (id INTEGER PRIMARY KEY CHECK (id=1), budget_sol REAL NOT NULL, realized_pnl_sol REAL NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS paper_positions (id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT NOT NULL, chain TEXT NOT NULL, entry_price REAL NOT NULL, current_price REAL NOT NULL, amount_sol REAL NOT NULL, peak_price REAL NOT NULL, opened_at INTEGER NOT NULL, strength REAL NOT NULL, wallet_count INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'open', closed_at INTEGER);
        CREATE TABLE IF NOT EXISTS paper_trades (id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT NOT NULL, chain TEXT NOT NULL, side TEXT NOT NULL, price REAL NOT NULL, amount_sol REAL NOT NULL, pnl_sol REAL NOT NULL DEFAULT 0, pnl_pct REAL NOT NULL DEFAULT 0, reason TEXT NOT NULL, strength REAL NOT NULL, wallet_count INTEGER NOT NULL, event_time_utc TEXT NOT NULL);
        """)
        self.conn.execute("INSERT OR IGNORE INTO paper_account(id,budget_sol,updated_at) VALUES(1,?,?)", (budget, int(time.time())))
        self.conn.commit()

    def balance(self) -> float:
        return float(self.conn.execute("SELECT budget_sol FROM paper_account WHERE id=1").fetchone()[0])

    def open_position(self, token: str, chain: str, price: float, strength: float, wallet_count: int) -> Position | None:
        if price <= 0 or self.balance() < self.entry_sol or self.has_open(token, chain):
            return None
        now = int(time.time())
        self.conn.execute("UPDATE paper_account SET budget_sol=budget_sol-?,updated_at=? WHERE id=1", (self.entry_sol, now))
        cur = self.conn.execute("INSERT INTO paper_positions(token,chain,entry_price,current_price,amount_sol,peak_price,opened_at,strength,wallet_count) VALUES(?,?,?,?,?,?,?,?,?)", (token, chain, price, price, self.entry_sol, price, now, strength, wallet_count))
        self.conn.execute("INSERT INTO paper_trades(token,chain,side,price,amount_sol,reason,strength,wallet_count,event_time_utc) VALUES(?,?,?,?,?,?,?,?,datetime('now'))", (token, chain, 'buy', price, self.entry_sol, 'weighted_cluster_entry', strength, wallet_count))
        self.conn.commit()
        return Position(cur.lastrowid, token, chain, price, price, self.entry_sol, price, now, strength, wallet_count)

    def has_open(self, token: str, chain: str) -> bool:
        return self.conn.execute("SELECT 1 FROM paper_positions WHERE token=? AND chain=? AND status='open'", (token, chain)).fetchone() is not None

    def close_position(self, position_id: int, price: float, reason: str) -> float:
        row = self.conn.execute("SELECT token,chain,entry_price,amount_sol,strength,wallet_count FROM paper_positions WHERE id=? AND status='open'", (position_id,)).fetchone()
        if not row or price <= 0:
            return 0.0
        token, chain, entry, amount, strength, wallet_count = row
        pnl = amount * (price / entry - 1.0)
        now = int(time.time())
        self.conn.execute("UPDATE paper_account SET budget_sol=budget_sol+?+?,realized_pnl_sol=realized_pnl_sol+?,updated_at=? WHERE id=1", (amount, pnl, pnl, now))
        self.conn.execute("UPDATE paper_positions SET current_price=?,status='closed',closed_at=? WHERE id=?", (price, now, position_id))
        self.conn.execute("INSERT INTO paper_trades(token,chain,side,price,amount_sol,pnl_sol,pnl_pct,reason,strength,wallet_count,event_time_utc) VALUES(?,?,?,?,?,?,?,?,?,?,datetime('now'))", (token, chain, 'sell', price, amount, pnl, pnl / amount * 100.0, reason, strength, wallet_count))
        self.conn.commit()
        return pnl

    def mark_prices(self, price_provider: Callable[[str, str], float], trail_activation: float = 0.25, trail_distance: float = 0.15, emergency_stop: float = -0.45) -> list[tuple[str, float, str]]:
        rows = self.conn.execute("SELECT id,token,chain,entry_price,current_price,amount_sol,peak_price,opened_at,strength,wallet_count FROM paper_positions WHERE status='open'").fetchall()
        closed = []
        for row in rows:
            pos = Position(*row)
            price = price_provider(pos.token, pos.chain)
            if price <= 0:
                continue
            peak = max(pos.peak_price, price)
            self.conn.execute("UPDATE paper_positions SET current_price=?,peak_price=? WHERE id=?", (price, peak, pos.id))
            pnl_pct = price / pos.entry_price - 1.0
            if pnl_pct <= emergency_stop:
                reason = 'emergency_stop_-45pct'
            elif peak / pos.entry_price - 1.0 >= trail_activation and price <= peak * (1.0 - trail_distance):
                reason = 'trailing_stop_15pct'
            else:
                continue
            closed.append((pos.token, self.close_position(pos.id, price, reason), reason))
        return closed

    def status(self) -> dict:
        row = self.conn.execute("SELECT budget_sol,realized_pnl_sol FROM paper_account WHERE id=1").fetchone()
        return {'balance_sol': float(row[0]), 'realized_pnl_sol': float(row[1]), 'open_positions': self.conn.execute("SELECT count(*) FROM paper_positions WHERE status='open'").fetchone()[0], 'entry_sol': self.entry_sol}
