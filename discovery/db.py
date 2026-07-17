"""SQLite database helpers for the discovery module.

Manages the 'candidate_wallets' and 'discovered_tokens' tables.
"""

import sqlite3
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Default path — shared with the Rust binary
DB_PATH = "sentinel.db"

def get_connection(db_path: str = DB_PATH) -> sqlite3.Connection:
    """Get a SQLite connection with WAL mode for concurrent reads."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn

def init_db(conn: sqlite3.Connection | None = None) -> None:
    """Create tables if they don't exist."""
    close = False
    if conn is None:
        conn = get_connection()
        close = True

    try:
        conn.executescript("""
            -- Candidate wallets discovered or manually seeded
            CREATE TABLE IF NOT EXISTS candidate_wallets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                address TEXT NOT NULL UNIQUE,
                source TEXT NOT NULL,          -- 'dex_screener', 'early_buyer', 'seed_list'
                token_count INTEGER DEFAULT 0,
                first_seen_slot INTEGER DEFAULT 0,
                avg_first_seen_slot REAL DEFAULT 0,
                discovered_at TEXT NOT NULL DEFAULT (datetime('now')),
                metadata TEXT,                 -- JSON blob for extra info
                status TEXT DEFAULT 'pending'  -- pending, tracked, dropped
            );

            -- Tokens that led to wallet discovery
            CREATE TABLE IF NOT EXISTS discovered_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token_address TEXT NOT NULL UNIQUE,
                token_symbol TEXT,
                token_name TEXT,
                source TEXT NOT NULL,
                liquidity_usd REAL DEFAULT 0,
                volume_h24 REAL DEFAULT 0,
                price_change_h24 REAL DEFAULT 0,
                discovered_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            -- Index for wallet lookups
            CREATE INDEX IF NOT EXISTS idx_candidate_wallets_status
                ON candidate_wallets(status);

            CREATE INDEX IF NOT EXISTS idx_candidate_wallets_source
                ON candidate_wallets(source);
        """)
        conn.commit()
        logger.info("Database initialised at %s", conn.execute("PRAGMA database_list").fetchone()[2])
    finally:
        if close:
            conn.close()

def insert_candidate_wallets(
    wallets: list[dict[str, Any]],
    source: str,
    conn: sqlite3.Connection | None = None,
) -> int:
    """Insert candidate wallets, skipping duplicates.

    Returns the number of new wallets inserted.
    """
    close = False
    if conn is None:
        conn = get_connection()
        close = True

    inserted = 0
    try:
        for w in wallets:
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO candidate_wallets
                       (address, source, token_count, first_seen_slot,
                        avg_first_seen_slot, metadata)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        w["address"],
                        source,
                        w.get("token_count", 0),
                        w.get("first_seen_slot", 0),
                        w.get("avg_first_seen_slot", 0.0),
                        w.get("metadata"),
                    ),
                )
                if conn.total_changes > 0:
                    inserted += 1
            except sqlite3.IntegrityError:
                continue
        conn.commit()
    finally:
        if close:
            conn.close()

    logger.info("Inserted %d/%d wallet(s) from source '%s'", inserted, len(wallets), source)
    return inserted

def insert_discovered_token(
    token: dict[str, Any],
    source: str,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Insert a discovered token, skipping duplicates."""
    close = False
    if conn is None:
        conn = get_connection()
        close = True

    try:
        conn.execute(
            """INSERT OR IGNORE INTO discovered_tokens
               (token_address, token_symbol, token_name, source,
                liquidity_usd, volume_h24, price_change_h24)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                token.get("address", token.get("token_address")),
                token.get("symbol", ""),
                token.get("name", ""),
                source,
                float(token.get("liquidity", {}).get("usd", 0) if isinstance(token.get("liquidity"), dict) else token.get("liquidity_usd", 0)),
                float(token.get("volume", {}).get("h24", 0) if isinstance(token.get("volume"), dict) else token.get("volume_h24", 0)),
                float(token.get("priceChange", {}).get("h24", 0) if isinstance(token.get("priceChange"), dict) else token.get("price_change_h24", 0)),
            ),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        if close:
            conn.close()

def get_candidate_wallets(
    status: str | None = "pending",
    limit: int = 100,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    """Fetch candidate wallets, optionally filtered by status."""
    close = False
    if conn is None:
        conn = get_connection()
        close = True

    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM candidate_wallets WHERE status = ? ORDER BY token_count DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM candidate_wallets ORDER BY token_count DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        if close:
            conn.close()

def mark_wallet_tracked(address: str, conn: sqlite3.Connection | None = None) -> None:
    """Mark a wallet as tracked (moved to the active watch list)."""
    close = False
    if conn is None:
        conn = get_connection()
        close = True

    try:
        conn.execute(
            "UPDATE candidate_wallets SET status = 'tracked' WHERE address = ?",
            (address,),
        )
        conn.commit()
    finally:
        if close:
            conn.close()
