"""SQLite schema for the scorer module.

Extends the DB with wallet scoring tables.
"""

import sqlite3
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

DB_PATH = "sentinel.db"

def get_connection(db_path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn

def init_scorer_tables(conn: sqlite3.Connection | None = None) -> None:
    """Create scorer-specific tables."""
    close = False
    if conn is None:
        conn = get_connection()
        close = True

    try:
        conn.executescript("""
            -- Wallet scoring results (Section 6)
            CREATE TABLE IF NOT EXISTS wallet_scores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet_address TEXT NOT NULL UNIQUE,
                tier TEXT NOT NULL,            -- 'A', 'B', 'C'
                edge_score REAL DEFAULT 0.0,
                payoff_ratio REAL DEFAULT 0.0,
                win_rate REAL DEFAULT 0.0,
                total_trades INTEGER DEFAULT 0,
                win_count INTEGER DEFAULT 0,
                loss_count INTEGER DEFAULT 0,
                realized_pnl_sol REAL DEFAULT 0.0,
                avg_win_sol REAL DEFAULT 0.0,
                avg_loss_sol REAL DEFAULT 0.0,
                tx_per_week REAL DEFAULT 0.0,
                window_start TEXT,
                window_end TEXT,
                last_scored_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            -- Per-trade breakdowns for a wallet (used for scoring)
            CREATE TABLE IF NOT EXISTS wallet_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet_address TEXT NOT NULL,
                signature TEXT NOT NULL,
                token_mint TEXT NOT NULL,
                venue TEXT,
                direction TEXT,                -- 'buy' or 'sell'
                amount_sol REAL DEFAULT 0.0,
                amount_token REAL DEFAULT 0.0,
                price_sol REAL DEFAULT 0.0,
                slot INTEGER DEFAULT 0,
                block_time INTEGER,
                simulated_fill_price_sol REAL,
                network_fee_sol REAL DEFAULT 0.0,
                realized_pnl_sol REAL DEFAULT 0.0,
                is_win BOOLEAN,
                UNIQUE(wallet_address, signature)
            );

            CREATE INDEX IF NOT EXISTS idx_wallet_scores_tier
                ON wallet_scores(tier);
            CREATE INDEX IF NOT EXISTS idx_wallet_scores_address
                ON wallet_scores(wallet_address);
            CREATE INDEX IF NOT EXISTS idx_wallet_trades_wallet
                ON wallet_trades(wallet_address);
        """)
        conn.commit()
        logger.info("Scorer tables initialised")
    finally:
        if close:
            conn.close()

def upsert_wallet_score(
    conn: sqlite3.Connection,
    wallet_address: str,
    tier: str,
    edge_score: float,
    payoff_ratio: float,
    win_rate: float,
    total_trades: int,
    win_count: int,
    loss_count: int,
    realized_pnl_sol: float,
    avg_win_sol: float,
    avg_loss_sol: float,
    tx_per_week: float,
    window_start: str | None = None,
    window_end: str | None = None,
) -> None:
    """Insert or update a wallet's score."""
    conn.execute(
        """INSERT OR REPLACE INTO wallet_scores
           (wallet_address, tier, edge_score, payoff_ratio, win_rate,
            total_trades, win_count, loss_count, realized_pnl_sol,
            avg_win_sol, avg_loss_sol, tx_per_week,
            window_start, window_end, last_scored_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
        (
            wallet_address, tier, edge_score, payoff_ratio, win_rate,
            total_trades, win_count, loss_count, realized_pnl_sol,
            avg_win_sol, avg_loss_sol, tx_per_week,
            window_start, window_end,
        ),
    )
    conn.commit()

def get_scored_wallets(
    conn: sqlite3.Connection,
    tier: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Fetch scored wallets, optionally filtered by tier."""
    if tier:
        rows = conn.execute(
            "SELECT * FROM wallet_scores WHERE tier = ? ORDER BY edge_score DESC LIMIT ?",
            (tier, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM wallet_scores ORDER BY edge_score DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]
