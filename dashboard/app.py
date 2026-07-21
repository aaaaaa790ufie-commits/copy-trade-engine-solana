#!/usr/bin/env python3
"""Sentinel — Dashboard (Streamlit).

Read-only local web UI over the Sentinel SQLite database.
Strictly read-only: no write path, no engine control.

Usage:
    streamlit run dashboard/app.py
"""

import sqlite3
from pathlib import Path

import streamlit as st

st.set_page_config(
    page_title="Sentinel Dashboard",
    page_icon="🔭",
    layout="wide",
)

DB_PATH = Path(__file__).parent.parent / "sentinel.db"


@st.cache_resource
def get_db():
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def main():
    st.title("🔭 Sentinel Dashboard")
    st.markdown("Read-only view of the Sentinel copy-trading engine status.")

    if not DB_PATH.exists():
        st.warning(f"Database not found at `{DB_PATH}`. Run discovery first.")
        return

    conn = get_db()

    # ── Overview metrics ──────────────────────────────────────────
    col1, col2, col3, col4 = st.columns(4)

    wallet_count = conn.execute(
        "SELECT COUNT(*) FROM candidate_wallets"
    ).fetchone()[0]
    tracked_count = conn.execute(
        "SELECT COUNT(*) FROM candidate_wallets WHERE status='tracked'"
    ).fetchone()[0]
    token_count = conn.execute(
        "SELECT COUNT(*) FROM discovered_tokens"
    ).fetchone()[0]
    scored_count = conn.execute(
        "SELECT COUNT(*) FROM wallet_scores"
    ).fetchone()[0]

    col1.metric("Wallets Discovered", wallet_count)
    col2.metric("Tracked", tracked_count)
    col3.metric("Tokens Scanned", token_count)
    col4.metric("Scored", scored_count)

    # ── Scored Wallets ────────────────────────────────────────────
    st.subheader("🏆 Wallet Scores (Tier A)")
    tier_a = conn.execute(
        """SELECT wallet_address, edge_score, payoff_ratio, win_rate,
                  total_trades, realized_pnl_sol, last_scored_at
           FROM wallet_scores WHERE tier='A'
           ORDER BY edge_score DESC LIMIT 20"""
    ).fetchall()

    if tier_a:
        st.dataframe(
            [dict(r) for r in tier_a],
            column_config={
                "wallet_address": "Address",
                "edge_score": st.column_config.NumberColumn(format="%.4f"),
                "payoff_ratio": st.column_config.NumberColumn(format="%.2f"),
                "win_rate": st.column_config.NumberColumn(format="%.2f"),
                "total_trades": "Trades",
                "realized_pnl_sol": st.column_config.NumberColumn(format="%.6f"),
                "last_scored_at": "Last Scored",
            },
            use_container_width=True,
        )
    else:
        st.info("No Tier A wallets yet — run scorer first.")

    # ── Candidate Wallets ─────────────────────────────────────────
    st.subheader("👛 Candidate Wallets")
    candidates = conn.execute(
        "SELECT address, source, token_count, status FROM candidate_wallets ORDER BY token_count DESC LIMIT 50"
    ).fetchall()
    if candidates:
        st.dataframe([dict(r) for r in candidates], use_container_width=True)

    # ── Recent Trades (placeholder) ──────────────────────────────
    st.subheader("📊 Trade History")
    trades = conn.execute(
        "SELECT * FROM wallet_trades ORDER BY block_time DESC LIMIT 20"
    ).fetchall()
    if trades:
        st.dataframe([dict(r) for r in trades], use_container_width=True)
    else:
        st.info("No trades recorded yet — the engine is in paper/DRY_RUN mode.")

    # ── System Status ────────────────────────────────────────────
    st.subheader("⚙️ System Status")
    st.code(
        "DRY_RUN=true (paper mode) · LIVE=false · Wallet: 0 SOL (unfunded)",
    )


if __name__ == "__main__":
    main()
