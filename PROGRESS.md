# Sentinel — Build Progress

## Overview

Sentinel is a self-hosted Solana smart-money copy-trading engine that discovers
smart wallets from public on-chain data, tracks their trades via free-tier RPC,
and selectively copies their trades with independent risk management.

**Binary**: `target/release/sentinel.exe` (3.7 MB, Rust 1.97.1)
**Python modules**: discovery, scorer, dashboard
**Database**: SQLite (`sentinel.db`)

---

## Phase 1 — Scaffold

**Status**: ✅ COMPLETE

- [x] `sentinel/` root directory
- [x] `.gitignore` — excludes `.env*`, `*.key`, `wallets/`, `target/`, `.venv/`, `*.db`
- [x] `config.toml` — full config schema with all 9 sections
- [x] `.env.example` — exact variable names from Section 0
- [x] `README.md` — documents DRY_RUN behaviour, free-tier constraint
- [x] Directory structure per Section 5
- [x] Git repo initialised (`main`, commit `ba6262e`)
- [x] Rust project bootstrapped (`cargo init`, single binary)
- [x] Rust binary compiles and runs (all 5 modules start + heartbeat)

---

## Phase 2 — Discovery

**Status**: ✅ COMPLETE (validated with live DexScreener API calls)

- [x] `discovery/` — Python module:
  - [x] `dex_screener.py` — DexScreener API client (trending, top gainers, search)
  - [x] `early_buyer.py` — early-buyer wallet reconstruction from transaction history
  - [x] `db.py` — SQLite tables: `candidate_wallets`, `discovered_tokens`
  - [x] `run_discovery.py` — CLI entry point
  - [x] `seed_wallets.txt` — empty, ready for manual seeds
- [x] Validated end-to-end: hits DexScreener, early-buyer extraction works, cross-referencing works, SQLite writes
- [x] Known: public RPC rate-limited (429); needs API keys

---

## Phase 3 — Ingest

**Status**: ✅ STRUCTURE COMPLETE, UNVALIDATED (needs API keys)

- [x] WS RPC pool implementation (`ingest.rs`):
  - [x] Provider registry from `.env` (Helius, Alchemy, QuickNode, GetBlock, public fallback)
  - [x] `WsPool` struct: connection lifecycle, failover, backoff
  - [x] `subscribe_program` — builds `logsSubscribe` JSON-RPC requests
  - [x] `connect_provider` — async WebSocket via tokio-tungstenite + rustls
  - [x] Writer/reader task split per connection
  - [x] Graceful "no RPC providers configured" fallback
- [x] `SwapEvent` struct + `Venue`/`SwapDirection` enums
- [x] Known program IDs: Pump.fun, PumpSwap, Raydium AMM v4, Raydium CPMM
- [ ] **`decode_swap_event()`** — STUB. Always returns `None`. Real instruction parsing not implemented.
- [ ] **WS validation** — impossible without API keys. WebSocket pool connects to nothing when .env is empty.

---

## Phase 4 — Scorer

**Status**: ⚠️ STUB — scoring math exists, but PnL is always 0.0

What exists:
- [x] `scorer/` — Python module (`run_scorer.py`, `db.py`, `__init__.py`)
- [x] `db.py` — SQLite tables: `wallet_scores`, `wallet_trades`
- [x] `compute_edge_score()` — full Section 6 formula implemented
- [x] `assign_tier()` — A/B/C logic based on edge + activity
- [x] Recency decay (last 7 days weighted 2x)
- [x] Activity filter (5-300 tx/week)
- [x] Cluster check stub

What's missing:
- [ ] **Trade PnL parsing** — `run_scorer.py` line 219-227: all trades recorded with `realized_pnl_sol: 0.0`. No swap instruction parsing to extract real PnL.
- [ ] **Never run end-to-end** — cannot score wallets without (a) API keys for RPC, (b) discovery having run first, (c) real PnL parsing.
- [ ] Scoring with all-zero PnL produces garbage scores (by definition all trades are "losses" of 0 SOL).

---

## Phase 5 — Filter + Risk

**Status**: ⚠️ STUB — pipeline compiles, no SQLite integration

What exists:
- [x] `filter.rs` — receives `SwapEvent`, tier-based routing (A→copy, B→watch, C→skip)
- [x] `TierCache` struct with interval refresh pattern
- [x] `risk.rs` — per-source-wallet allocation cap, max concurrent positions
- [x] Token security pre-check stubs (LP lock, mint authority, top-10 holder %)
- [x] Produces `ExecCommand` for executor
- [x] All modules wired in `main.rs` via tokio mpsc channels

What's missing:
- [ ] **SQLite reader** — `TierCache` reads from empty `HashMap`. Should query `wallet_scores` table.
- [ ] **Real security checks** — `security_ok` hard-coded to `true`.
- [ ] **Position tracking** — `open_positions` increments but never decrements.

---

## Phase 6 — Executor (Instruction Encoding)

**Status**: ❌ NOT IMPLEMENTED — all 4 venue builders return errors

What exists:
- [x] `executor.rs` — `ExecCommand` struct, program ID constants
- [x] `build_pump_fun_instruction()` — **returns `Err("not yet implemented")`** (discriminator + account layout documented in comments)
- [x] `build_pump_swap_instruction()` — **returns `Err("not yet implemented")`**
- [x] `build_raydium_amm_v4_instruction()` — **returns `Err("not yet implemented")`**
- [x] `build_raydium_cpmm_instruction()` — **returns `Err("not yet implemented")`**
- [x] `build_jito_bundle()` — no-op (returns input unchanged)
- [x] `estimate_tip()` — placeholder (1000 lamports)
- [x] Spawn function — commented-out event loop, heartbeat only

What's missing:
- [ ] **ALL instruction encoding** — no venue has working instruction builders.
- [ ] **Jupiter fallback** — configured in config.toml but no code wired.

---

## Phase 7 — Position Manager + Telemetry

**Status**: ❌ NOT IMPLEMENTED — TP/SL logic is all TODO comments

What exists:
- [x] `position_mgr.rs` — `Position` struct with all fields
- [x] Loop ticks at configured interval
- [x] `tracing_subscriber` configured in main.rs
- [x] Heartbeat logging from every module

What's missing:
- [ ] **Stop-loss check** — commented out (TODO)
- [ ] **Trailing stop logic** — commented out (TODO)
- [ ] **Auto-sell trigger** — no executor channel connected
- [ ] **Current price fetch** — no RPC call to get pool state

---

## Phase 8 — Live-Submit Path

**Status**: 🔲 NOT STARTED

Requires (in order):
- [ ] Wallet funding (user's responsibility — 0 SOL currently)
- [ ] Venue instruction encoding (Phase 6)
- [ ] Jito bundle submission via Block Engine API
- [ ] Dynamic tip estimation
- [ ] Double-gate: `dry_run=false` + `live=true`
- [ ] Error handling + retry logic

---

## Phase 9 — Dashboard

**Status**: ✅ COMPLETE

- [x] `dashboard/app.py` — Streamlit read-only UI
- [x] Metrics: wallets discovered, tracked, tokens scanned, scored
- [x] Tier A wallet table with edge_score, payoff_ratio, win_rate
- [x] Candidate wallets list
- [x] Trade history table
- [x] System status display
- [x] Strictly read-only — no write path
- [x] Usage: `streamlit run dashboard/app.py`

---

## Pre-flight Status (Section 0)

- [ ] **API keys in `.env`**: ⚠️ NONE. `cp .env.example .env` done but empty.
- [ ] **Manual seed list**: `discovery/seed_wallets.txt` is empty.
- [ ] **Wallet funded**: 0 SOL — user responsibility per original goal.

## Free-Tier Accounts (Section 1 of follow-up goal)

| Provider | WS | Status |
|----------|----|--------|
| Helius   | ✅ | ❌ not signed up yet |
| Alchemy  | ✅ | ❌ not signed up yet |
| QuickNode| ✅ | ❌ not signed up yet |
| GetBlock | ✅ | ❌ not signed up yet |
| Ankr     | ❌ (HTTP only) | ❌ not signed up yet |

## Known Issues

1. **Phase 6 — instruction encoding**: highest-risk item. All venue builders return errors.
   Cross-check against real `getTransaction` responses required before marking anything
   other than UNVERIFIED.
2. **Phase 4 — PnL parsing**: scorer records all trades with 0.0 PnL. Need swap
   instruction decoding to extract real PnL.
3. **Phase 5 — no SQLite**: filter reads from empty HashMap, not from `wallet_scores`.
4. **No Jupiter fallback**: configured in config.toml but no code exists.
5. **Phase 6,7,8 build on each other**: Executor → Position Mgr → Live-Submit
   must be built sequentially due to dependency chain.
