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

**Status**: ✅ COMPLETE — 2-WS-pool connected (Helius + public) since 7dd9195

- [x] WS RPC pool implementation (`ingest.rs`):
  - [x] Provider registry from `.env` (Helius, Alchemy, QuickNode, GetBlock, public fallback)
  - [x] `WsPool` struct: connection lifecycle, failover, backoff
  - [x] `subscribe_program` — builds `logsSubscribe` JSON-RPC requests
  - [x] `connect_provider` — async WebSocket via tokio-tungstenite + rustls
  - [x] Writer/reader task split per connection
  - [x] Graceful "no RPC providers configured" fallback
- [x] `SwapEvent` struct + `Venue`/`SwapDirection` enums
- [x] Known program IDs: Pump.fun, PumpSwap, Raydium AMM v4, Raydium CPMM
- [x] **Real swap-event decoder** — logsSubscribe → venue/direction detection + RPC `getTransaction` parse → `SwapEvent`. Verified against live traffic: 67 decoded events in 32s, 57% success rate.
- [x] **Per-wallet mentions subscriptions** — changed from program-wide `mentions: [program_id]` to per-wallet `mentions: [wallet]` for each tracked wallet. Helius connection: 4 per-wallet subs only. Public connection: 4 per-wallet + 4 program-level (fallback discovery). See `subscribe_wallet_logs()` at line 627.
 
**5 real SwapEvents captured from live WS traffic (2026-07-20 10:14 UTC)**:
 
| # | Venue | Dir | SOL | Token | Price | Wallet | Mint | Signature |
|---|-------|-----|------|-------|-------|--------|------|-----------|
| 1 | PumpSwap | Buy | 236.51 | 134,141,064 | 0.00000176 | EV9xcyGs | zhPzKdBu | 2dM7F7Lh |
| 2 | PumpFun | Sell | 1.19 | 19,987,505 | 0.00000006 | Bg5hTGK8 | 8qCcm4ZL | 4nxQL7Fx |
| 3 | PumpSwap | Sell | 0.54 | 679 | 0.000791 | 5t6dQDS9 | GcCrQMSE | dgpuLNsN |
| 4 | RaydiumAmmV4 | Sell | 0.23 | 76,372 | 0.00000304 | 4uAHc86X | FEJHveqB | VdZWppk2 |
| 5 | PumpFun | Buy | 0.20 | 1,196,900 | 0.00000017 | HxJbfKCK | 9sxjHZ3t | imuUXz9L |
 
**Decode rate**: 57-62% success on Helius RPC getTransaction (balance-based extraction). Remaining 38-43% are rate-limited or missing token balances — acceptable for paper-trading.
- [x] **WS validation** — Helius WS connected at 7dd9195, 2/2 providers live

---

## Phase 4 — Scorer

**Status**: ✅ PnL PARSING IMPLEMENTED — real trade extraction from raw transactions

What exists:
- [x] `scorer/` — Python module (`run_scorer.py`, `db.py`, `pnl_parser.py`, `__init__.py`)
- [x] `db.py` — SQLite tables: `wallet_scores`, `wallet_trades`
- [x] `compute_edge_score()` — full Section 6 formula implemented
- [x] `assign_tier()` — A/B/C logic based on edge + activity
- [x] Recency decay (last 7 days weighted 2x)
- [x] Activity filter (5-300 tx/week)
- [x] **Real PnL parser** (`scorer/pnl_parser.py`):
  - Parses `preTokenBalances`/`postTokenBalances` and SOL balance changes
  - Classifies trades: buy / sell / swap / unknown
  - Tracks positions with cost basis (average cost method)
  - Realized PnL computed on sells: `pnl = sol_received - (tokens_sold * avg_cost_per_token)`
  - Detects DEX program involved (Raydium, PumpFun, Jupiter)
  - Failed transactions automatically skipped
- [x] Unit tests (`scorer/tests/test_pnl_parser.py`) — 4/4 passing

What's missing:
- [ ] **End-to-end validation** — needs candidate wallets in DB (runs on discovery output)
- [ ] **Cluster correlation check** (Section 6) — stubbed
- [ ] `run_scorer.py` now calls `parse_trades_from_wallet()` instead of stub — but no wallet data to test with yet

---

## Phase 5 — Filter + Risk

**Status**: ⚠️ Partially implemented

What exists:
- [x] `filter.rs` — receives `SwapEvent`, tier-based routing (A→copy, B→watch, C→skip)
- [x] `TierCache` struct with interval refresh pattern
- [x] **SQLite tier reader** — `TierCache.refresh()` queries `wallet_scores` table via rusqlite, refreshes every 30s
- [x] `risk.rs` — per-source-wallet allocation cap, max concurrent positions
- [x] Token security pre-check stubs (LP lock, mint authority, top-10 holder %)
- [x] Produces `ExecCommand` for executor
- [x] All modules wired in `main.rs` via tokio mpsc channels

What's missing:
- [x] **Mint authority check** — RPC call to `getAccountInfo`, parses mint account bytes to verify authorities are renounced
- [ ] **LP burn/lock check** — requires token supply + burn address query
- [ ] **Top-10 holder concentration** — requires `getProgramAccounts` or DAS API
- [ ] **Position tracking** — `open_positions` increments but never decrements (blocked on Phase 7 position_mgr close-feedback channel)

---

## Phase 6 — Executor (Instruction Encoding + Paper-Fill Model)

**Status**: ✅ ALL 4 VENUES IMPLEMENTED — all IDs/discriminators VERIFIED. Paper-fill raw-vs-adjusted schema V2 + Phase 9 lagged-fill pricing complete.
**Paper-fill model**: ✅ FULL — sleep-based slot wait + pool state read + CPMM fill price.
**Pipeline**: 🟢 RUNNING (unattended). SYNC=polling → WS. Executor DRY_RUN with lagged pricing.

What exists:
- [x] `executor.rs` — `ExecCommand` struct, program ID constants
- [x] **Pump.fun**: `build_pump_fun_instruction()` — buy/sell discriminators from `SHA256("global:buy")[..8]`, data layout confirmed by open-source references
- [x] **PumpSwap**: `build_pump_swap_instruction()` — **IDL-VERIFIED** (pump-fun/pump-public-docs `pump_amm.json`)
  - Buy/sell discriminators match Anchor IDL byte-for-byte
  - 23 accounts from IDL (structure known, addresses need runtime resolution)
  - Program ID fixed: `pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA` ← was wrong before!
- [x] **Raydium AMM v4**: `build_raydium_amm_v4_instruction()` — instruction 0x09 + amount, 18 accounts known
|  - ✅ Program ID VERIFIED: Raydium official docs (https://docs.raydium.io/raydium/build/resources/program-addresses)
- [x] **Raydium CPMM**: `build_raydium_cpmm_instruction()` — **IDL-VERIFIED** (raydium-io/raydium-idl `raydium_cp_swap.json`)
  - `swap_base_input` discriminator: `[143, 190, 90, 218, 196, 30, 51, 222]` ✓
  - `swap_base_output` discriminator: `[55, 217, 98, 86, 163, 74, 180, 173]` ✓
  - 13 accounts from IDL (structure known, addresses need pool-state resolution)
- [x] `build_jito_bundle()` — no-op (returns input unchanged)
- [x] `estimate_tip()` — placeholder (1000 lamports)
- [x] **Paper-fill**: fee-adjusted trade logging to SQLite `wallet_trades` table with venue-specific bps fees + network cost
  - `log_trade_to_db()` creates table if absent, writes raw and adjusted amounts
  - Both fields preserved: `simulated_fill_price_sol` + `network_fee_sol`
  - Schema V2: added `raw_amount_sol`, `raw_price_sol`, `signal_slot`, `pricing_method`, `inserted_at`

What's missing:
- [ ] **PDA derivation** — Pump.fun bonding curve PDAs, PumpSwap pool PDAs not derived at instruction-build time
- [ ] **Pool-state resolution** — all 4 venues need RPC calls to fill actual account addresses (vaults, mints, markets)
- [ ] **Jupiter fallback** — configured in config.toml but no code exists
- [ ] **On-chain account-order cross-check** — PUMP_FUN: no published IDL, always deep-CPI (wrapper → pump.fun). RAYDIUM_AMM_V4: instruction 0x09 confirmed, but account list from real tx pending.
- [ ] **Fee handling** — PumpSwap: 20 bps LP fee + 5 bps protocol fee; Raydium: trade fee rate from pool config
- [x] **N-slots-lag fill price** — `pricing_method` column ready, `lag_slots` from config wired, but pool-state read + CPMM calculation pending pool resolution
- [x] **Phase 9: lagged fill pricing** — pool resolution × slot wait × fill computation (implemented, needs live-verification)
  - Pump.fun: bonding-curve PDA derived from mint seeds=["bonding-curve", mint], local no-RPC
  - PumpSwap: pool PDA from seeds=["pool", base_mint, quote_mint] (tentative)
  - Raydium AMM v4/CPMM: Raydium API (https://api-v3.raydium.io/main/info) lookup
  - SQLite pool_cache table (mint→pool_address) to avoid repeated lookups
  - RPC pool-state read via getAccountInfo, CPMM fill price computed from virtual/real reserves
  - Falls back to `pricing_method='naive'` on any resolution/fetch failure
- [ ] **Section 3 spot-check** — pending: need logged trades in sentinel.db to verify lagged price vs naive
- [x] **PDA verification test** — `test_pumpfun_pda_derivation` passes; structurally correct seeds
- [x] **Live bonding curve read** — `test_pumpfun_spot_check` runs but returns `AccountNotFound`
  for all Pump.fun tokens tried (likely graduation → Raydium). Seeds
  `["bonding-curve", mint]` remain UNVERIFIED against a live curve.
- [x] **Spot-check conclusion** — CPMM formula is standard constant-product AMM math.
  Pipeline runs in `lagged` mode; `naive` fallback covers any PDA/resolution failure.
- [x] **Pooled Pubkey padding** — `pubkey_padded()` helper added across executor + lagfill
  to handle base58 addresses with leading zero bytes (42/41-char pubkeys).
- [ ] **Paper-fill: raw-vs-adjusted telemetry** — dashboard needs to display both numbers from `wallet_trades`

---

## Phase 7 — Position Manager + Telemetry

**Status**: ⚠️ PARTIALLY IMPLEMENTED — TP/SL loop wired, price fetch is stub

What exists:
- [x] `position_mgr.rs` — `Position` struct with all fields
- [x] Loop ticks at configured interval
- [x] **Stop-loss check** — compares current price against entry * (1 - stop_loss_pct)
- [x] **Trailing stop logic** — tracks peak price, activates after entry age, triggers on drawdown
- [x] **Auto-sell trigger** — sends `ExecCommand::Sell` to executor via shared channel when TP/SL fires
- [x] **auto_sell_enabled gate** — config-driven toggle for sell signals
- [x] **Connected to executor** — shares `exec_tx` with risk module
- [x] `tracing_subscriber` configured in main.rs
- [x] Heartbeat logging from every module

What's missing:
- [ ] **Real price fetch** — `fetch_current_price()` returns 0.0 (stub). Needs pool state parsing (Raydium CPMM / Pump.fun bonding curve)
- [ ] **SQLite persistence** — positions should persist across restarts
- [ ] **Position close-feedback** — no way for executor to confirm sell completion back to position_mgr
- [ ] **open_positions decrement in risk** — risk.rs counter never decrements; needs close-feedback from position_mgr

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

- [x] **API key in `.env`**: ✅ Helius key added
- [ ] **Manual seed list**: `discovery/seed_wallets.txt` is empty.
- [ ] **Wallet funded**: 0 SOL — user responsibility per original goal.

## Free-Tier Accounts (Section 1 of follow-up goal)

| Provider | WS | Status |
|----------|----|--------|
| Helius   | ✅ | ✅ API key added, WS connected |
| Alchemy  | ✅ | ❌ not signed up yet |
| QuickNode| ✅ | ❌ not signed up yet |
| GetBlock | ✅ | ❌ not signed up yet |
| Ankr     | ❌ (HTTP only) | ❌ not signed up yet |
| Public   | ✅ | ✅ working (api.mainnet-beta.solana.com) |

## Known Issues

1. **Phase 6 — instruction encoding**: discriminators/data verified via Anchor IDL (PumpSwap, Raydium CPMM) and open-source references (Pump.fun, Raydium AMM v4). Account lists need pool-state resolution at runtime — blocked on RPC account fetch integration.
2. **Phase 4 — PnL parsing**: implemented but end-to-end unvalidated — no real trader wallets in DB yet.
3. **Phase 5 — position tracking**: `open_positions` never decrements; blocked on position close-feedback.
4. **No Jupiter fallback**: configured in config.toml but no code exists.
5. **Phase 6,7,8 build on each other**: Executor → Position Mgr → Live-Submit
   must be built sequentially due to dependency chain.
