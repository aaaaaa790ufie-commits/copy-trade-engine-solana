# Sentinel — Build Progress

## Pre-flight Status (Section 0)

- [ ] **API keys in `.env`**: ⚠️ NONE PROVIDED. No `.env` file found.
      Proceeding with build work that doesn't need RPC access (Phase 1-2 scaffold,
      Rust scaffolding, Python discovery code skeleton). Phases requiring RPC
      (Ingest, Scorer) will be built but can't be validated until keys are added.
- [ ] **Seed list** (`discovery/seed_wallets.txt`): ⚠️ NOT PROVIDED.
      Proceeding on automated discovery only (DexScreener + early-buyer reconstruction).
- [ ] **Rust toolchain**: ✅ Installed (rustc 1.97.1, cargo 1.97.1).
- [ ] **Python 3.11.9**: ✅ Available (pip, uv installed).
- [ ] **Network outbound**: ⚠️ Download worked, but curl via git-bash had issues.
      May need PowerShell for reliable HTTP calls during build.

---

## Phase 1 — Scaffold

**Status**: ✅ COMPLETE

- [x] Project directory structure created
- [x] `.gitignore` (covers `.env*`, `*.key`, `wallets/`, Rust/Python artifacts)
- [x] `.env.example` with exact variable names from Section 0
- [x] `config.toml` schema (all sections: rpc, discovery, scoring, simulation, risk, executor, position_manager, telemetry)
- [x] `README.md` with architecture overview and safety notes
- [x] `PROGRESS.md` (this file)
- [x] Rust toolchain installed (rustc 1.97.1)
- [x] Git repo initialized

## Phase 2 — Discovery

**Status**: ✅ COMPLETE (validated with live API calls)

- [x] `discovery/` — Python module with:
  - [x] `dex_screener.py` — DexScreener client (trending tokens, top gainers, search, pairs)
  - [x] `early_buyer.py` — Early-buyer reconstruction from tx history via RPC
  - [x] `db.py` — SQLite schema + helpers for candidate wallets / discovered tokens
  - [x] `run_discovery.py` — Entry point with CLI args
  - [x] `__init__.py`, `requirements.txt`, `seed_wallets.txt` (empty)
- [x] Validated end-to-end: DexScreener API reachable, early-buyer extraction works,
      cross-referencing works, SQLite writes work
- [x] Public RPC hit 429 rate limit (expected — resolves when API keys are added)
- [x] `sentinel.db` created with `candidate_wallets` + `discovered_tokens` tables

## Phase 3 — Ingest

**Status**: ✅ COMPLETE (core structure — awaits API keys for live validation)

- [x] WS RPC pool implementation (`ingest.rs`):
  - [x] Provider registry from .env (Helius, Alchemy, QuickNode, GetBlock)
  - [x] Priority-weighted provider selection
  - [x] WebSocket connection manager with `tokio-tungstenite` via rustls
  - [x] `logsSubscribe` for all 4 tracked program IDs (Pump.fun, PumpSwap, Raydium AMM v4, CPMM)
  - [x] Writer/reader task split per connection
  - [x] Graceful fallback when no keys configured (helpful log message)
- [x] SwapEvent struct + venue/direction enums
- [x] `decode_swap_event()` stub (real instruction parsing → Phase 6)
- [x] Graceful: "no RPC providers configured" warning instead of crash
- [ ] ⚠️ Cannot validate without API keys in .env (rate-limited public RPC doesn't support WS)

## Phase 4 — Scorer

**Status**: 🔧 IN PROGRESS

## Phase 5 — Filter + Risk

**Status**: 🔲 NOT STARTED

## Phase 6 — Executor

**Status**: 🔲 NOT STARTED

## Phase 7 — Position Manager + Telemetry

**Status**: 🔲 NOT STARTED

## Phase 8 — Live-Submit Path

**Status**: 🔲 NOT STARTED

## Phase 9 — Dashboard

**Status**: 🔲 NOT STARTED

---

### Notes

- Build started: current session
- Rust hot-path is a single `cargo` package, internal modules via tokio mpsc.
- Python modules are separate processes (scheduled jobs, not long-running servers).
- No Redis/Kafka — SQLite is the only cross-language hop.
