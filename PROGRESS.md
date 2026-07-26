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

---

# Hardening passes (`gmgn/` module)

Scope: `gmgn/` in full. Out of scope: the Rust crates (`ingest/`, `executor/`,
`filter/`, `risk/`, `scorer/`, `position_mgr/`, `telemetry/`), `target/`, `.venv/`.
Off-limits without approval: the paper-only nature of the engine, the contents of
`sentinel.db`, and the risk-parameter defaults.

## Pass 1 — 2026-07-26

Read end to end: `paper_engine.py`, `webapp.py`, `telegram_bot.py`, `config.py`,
`supervisor.py`, `tunnel.py`, `webapp/index.html`.

### Found and fixed

| # | Severity | Finding | Fix | Commit |
|---|---|---|---|---|
| 1 | High | Re-entering a token traded before raised `IntegrityError` on the `token_mint` primary key, killed the process, and the supervisor restarted into the same still-live signal — a crash loop with no stop-loss coverage | `enter()` reopens the row via `ON CONFLICT DO UPDATE`; history stays in `paper_trades` | `6771f93` |
| 2 | High | Any transient error out of `cycle()` terminated the poll loop | `run_forever()` rolls back, journals an `ERROR` event, retries; escalates only after `GMGN_MAX_CYCLE_FAILURES` | `6771f93` |
| 3 | High | `refresh_wallet_stats` rewrote a dormant wallet's win rate to a synthetic `0.49` so `cleanup_wallets` would permanently blacklist it — an 80% wallet banned for not buying recently, contradicting that function's own docstring | Ineligible wallets are parked with `active=0`, keep their real win rate, and are reactivated when they trade again | `a2d7e16` |
| 4 | High | Mints and wallet addresses from the GMGN API were stored raw and interpolated into the Mini App's HTML — stored XSS in a panel served over a public tunnel | Escaping moved inside `short()`; engine rejects non-base58 at the feed boundary | `714cafa` |
| 5 | High | An unpriced position was displayed at `peak_price`, the best price ever seen — a rugged token showed `+200%` | Valued at cost, with `priced: false` | `c44fe6a` |
| 6 | High | The bot reset its cursor to `MAX(id)` on start, silently dropping every event raised while it was down, including `BANKRUPT` and `EXIT` | Cursor persisted to `bot_state.json`; long outages summarised | `885e560` |
| 7 | Medium | `exits()` raised `ZeroDivisionError` on a zero entry price, aborting the sweep for every later position in the chain | Row skipped with an error log | `ea43add` |
| 8 | Medium | A delisted token's position never closes, locking its stake | `STUCK` event, throttled; valuation deferred to the operator (ISSUES.md #4) | `ea43add` |
| 9 | Medium | `token_price` could return a negative mark straight into the P&L calculation | Treated as unavailable | `ea43add` |
| 10 | Medium | `_price_cache` and the webapp's `_prices` grew unbounded | Both bounded; `merge_marks()` rebuilds from open positions | `ea43add`, `c44fe6a` |
| 11 | Medium | Elite buy call-outs only fired for makers seen for the first time, so a known 90% wallet never triggered one | Eligibility from `wallet_watch`; bounded by heartbeat, window and a per-cycle cap | `1d61602` |
| 12 | Medium | A failed `sendMessage` advanced the bot's cursor past the undelivered event | Cursor holds; event retried | `885e560` |
| 13 | Medium | The bot opened the database read-write, and a wrong path silently created an empty one | `mode=ro`, refuses to start if missing | `885e560` |
| 14 | Medium | The price refresher leaked a connection whenever its query raised | `try/finally` | `c44fe6a` |
| 15 | Medium | `_prices` was cleared then repopulated under the lock, so readers saw an empty dict mid-swap | Atomic rebind | `c44fe6a` |
| 16 | Medium | `.env` values kept their trailing `# comment`, so `POLL=15 # fast` silently fell back to the default | Stripped for unquoted values, preserved inside quotes | this pass |
| 17 | Medium | `enter()` and `save_token_scores()` duplicated the cluster computation, so `/weights` could drift from the entry decision | Unified in `cluster()`/`score_of()` | `ea43add` |
| 18 | Low | `supervise()` logged a deliberate restart as a crash and inflated its backoff | `stop(expected=True)` | this pass |
| 19 | Low | `supervise()` raised `ValueError` if called off the main thread | Signal handlers installed conditionally | this pass |
| 20 | Low | Bare `except: pass` around price parsing swallowed `KeyboardInterrupt` | Narrowed to `(TypeError, ValueError)` | `ea43add` |
| 21 | Low | `Child.stop()` never reaped a killed process | `wait()` after `kill()` | this pass |

### Verification

```
$ python -m unittest test_paper_engine
Ran 74 tests in 2.609s
OK
```

Tests went from 19 to 74. Beyond the new coverage, tightening address validation
exposed that several **existing** tests used placeholder addresses (`"MINTX"`,
`"w1"`): four failed outright, and others would have started passing vacuously,
since `cluster()` returns a `defaultdict` and an empty result still satisfied
`score == 0`. All fixtures now use real base58 addresses and assert presence
before asserting values.

Live checks, not inferred:

- One real cycle against the GMGN API: `[cycle] 4.4s wallets=1206 open=0`, no errors.
- `/api/overview` and `/api/health` both 200 against the real database.
- Supervisor restart semantics driven directly: an expected restart logged
  `restarting bot as requested` with `restarts=0, backoff=3s`; a killed child
  logged `exited with code 1 — restart #1` with backoff growing 3s → 6s.
- UI escaping verified by extracting `esc()`/`short()` from the page and running
  them under node against an `<img onerror>` payload.

### Still open

Four items are in `ISSUES.md` — they are real, but each changes trading
behaviour, mutates existing data, or needs a judgement that is the operator's:
the 5551 wrongly-blacklisted wallets (remediation script written, not run), the
strategy's unprofitability at `ENTRY_SCORE=0.25`, unbounded table growth, and how
to value a delisted position.

**Confidence that `gmgn/` is production-ready: medium.** The crash paths that
cost real money are fixed and pinned by tests, but this is one pass, and passes 2+
have not yet run.

## Pass 2 — 2026-07-26

Read end to end: `tunnel.py`, `mass_discovery.py`, `monitor.py`, plus a re-read of
everything changed in Pass 1.

### Found and fixed

| # | Severity | Finding | Fix | Commit |
|---|---|---|---|---|
| 22 | High | `gmgn_cli` ran the subprocess with `text=True`, decoding with the locale codec. On this cp1251 machine any emoji or CJK character in a token name raised `UnicodeDecodeError` — on the engine's main API path | Explicit `encoding="utf-8", errors="replace"` | `f7e87e2` |
| 23 | High | `mass_discovery` spawned `gmgn-cli` with no `env=`, so it used machine-wide credentials rather than the project's `.env` — contrary to the requirement that every API key stay local | Shares `gmgn_cli()` with the engine | `f7e87e2` |
| 24 | High | Win rate arriving under the `win_rate` spelling was never scaled: `"winrate" in "win_rate"` is `False`, so 75% parsed as `75.0`, clearing the ≥0.90 elite gate and rendering as 7500% | Underscore-insensitive check; out-of-range values refused | `c677c24` |
| 25 | Medium | A tunnel reader thread from a previous attempt could publish its dead hostname after a newer attempt cleared it — cloudflared is tried once per protocol, so this was the normal path | Per-spawn generation token | `f7e87e2` |
| 26 | Medium | Tunnel readiness was inferred from a log line, which the module's own docstring records as unreliable (HTTP 530) | URL probed against `/api/health` before publication | `f7e87e2` |
| 27 | Medium | `api_equity_curve` used `ORDER BY id LIMIT ?`, taking the *oldest* N trades, so past the limit the chart froze on early history | Takes the tail; pre-window trades folded into the opening value | this pass |
| 28 | Medium | `monitor.py` invoked a bare `gmgn-cli` (unresolvable by CreateProcess on Windows), decoded with the locale codec, and used machine-wide credentials | Shares `gmgn_cli()`; tunables via `config.py` | `c677c24` |
| 29 | Medium | `active=0` (parked) was introduced in Pass 1 but nothing surfaced it, so the pool could shrink invisibly | Reported in `/wallets` and the panel; `wallets_active` renamed `wallets_qualified` | `c677c24` |
| 30 | Medium | Addresses were not validated in `mass_discovery`, though its output is a committed file | `valid_address` on feed and seed input | `f7e87e2` |
| 31 | Low | `_cli_retry(retries=0)` would `raise None` | Guarded | `f7e87e2` |
| 32 | Low | No progress output during the long stats phase, so a full run was indistinguishable from a hang | Periodic progress line | `f7e87e2` |
| 33 | Low | README claimed `mass_discovery` "applies the 7-day activity gate"; its default is 0, so it does not | Both README and docstring state each gate's real default | `f7e87e2` |

### Verification

```
$ python -m unittest test_paper_engine
Ran 94 tests in 5.300s
OK
```

The decoding bug (#22) was found by running `mass_discovery` against the live API,
not by reading: three feed calls died and the run yielded 1 qualified wallet.
After the fix the same command yields 4. The engine issues that call every poll.

Full stack run with the tunnel, and the new probe earned its place immediately:

```
INFO  tunnel reports ready: https://jgmta-...free.pinggy.net
WARN  https://jgmta-...free.pinggy.net never served a request
      ([SSL: DECRYPTION_FAILED_OR_BAD_RECORD_MAC]) — treating as failed
INFO  tunnel reports ready: https://hrqqr-...run.pinggy-free.link
INFO  Mini App published at https://hrqqr-...run.pinggy-free.link
INFO  restarting webapp as requested
INFO  mini app on http://127.0.0.1:8770 (auth required)
```

The first URL was live by the provider's own account and did not serve; before this
pass it would have become the Mini App button. The second passed and was published.
`restarting webapp as requested` is the Pass 1 expected-restart fix, visible in
production rather than only in a harness.

Auth checked from outside, through the public origin:

| Endpoint | Result |
|---|---|
| `/api/health` | 200 — unauthenticated by design; the probe uses it |
| `/api/overview`, `/api/trades`, `/api/wallets` | 401 |
| `/api/overview` with forged `initData` | 401 |
| `/` (static page) | 200 |

Equity curve cross-checked against the account: ends at 0.03886 = initial +
realised, at `limit=300` and at `limit=2`, where it correctly reports `truncated`.

### Still open

`ISSUES.md` is unchanged: the same four items, all needing an operator decision.

**Confidence that `gmgn/` is production-ready: medium-high.** Pass 2 found three
high-severity bugs, two of them only visible by running against the live API rather
than reading — which is why the stopping condition is two consecutive clean passes,
not one. Pass 3 has not yet run.

## Pass 3 — 2026-07-26

Read end to end: `run_engine.py`, `webapp.py` (handler and routes), `telegram_bot.py`,
`webapp/index.html`, plus a re-read of everything changed in Pass 2.

### Found and fixed

| # | Severity | Finding | Fix | Commit |
|---|---|---|---|---|
| 34 | Medium | `import_old_wallets` skipped the blacklist check and ran on every start, so a wallet banned by `cleanup_wallets` returned from the legacy tables on the next restart, was re-queried and re-banned — a churn loop spending API calls on known-bad wallets, competing with the stop-loss path | One `_admit()` helper that rejects malformed addresses and skips banned ones | `28474bf` |
| 35 | Medium | Neither startup import validated addresses, so anything in the legacy tables or seed file reached `wallet_watch` and the panel | `valid_address` on both paths, with a count of what was dropped | `28474bf` |
| 36 | Medium | Static-file containment was a string prefix test: with a root of `.../gmgn/webapp`, a path resolving into `.../gmgn/webapp-anything` passed | `Path.is_relative_to` | `436812f` |
| 37 | Medium | API errors returned `str(e)`, which can disclose filesystem paths over the public tunnel | Detail to the log, generic message to the client | `436812f` |
| 38 | Medium | `reply()` caught only `OperationalError`. Anything else escaped to the poll loop — and since the update offset advances before the reply is sent, the command was lost and the user saw silence | All failures answer; one bad reply no longer abandons the updates behind it | `436812f` |
| 39 | Low | `ThreadingHTTPServer` spawns a thread per connection uncapped, and a half-open connection pinned one forever | `Handler.timeout` | `436812f` |
| 40 | Low | `SEEDS_PATH` read `os.getenv` directly, bypassing the repo `.env` | Routed through `config.get` | `28474bf` |

### Verification

```
$ python -m unittest test_paper_engine
Ran 109 tests in 5.018s
OK
```

Path traversal checked against a running server rather than only in unit tests:

| Request | Result |
|---|---|
| `/../config.py`, `/../../.env`, `/..%2f..%2f.env` | 404 |
| `/C:/Windows/win.ini`, `/../webapp.py`, `/../../sentinel.db` | 404 |
| `/`, `/index.html` | 200, page renders |

Full stack launched with engine, bot and Mini App:

- Bot token verified live via `getMe` → `@solrobinbot`.
- Tunnel published; the probe again rejected one pinggy URL on TLS before accepting
  the next, so the button was never pointed at a dead origin.
- Menu button confirmed via `getChatMenuButton`: type `web_app`, pointing at the live
  URL; all seven commands registered via `getMyCommands`.
- Through the public origin: page 200, `/api/health` 200, `/api/overview` 401,
  forged `initData` 401.
- The 401 auth wall rendered in a real browser rather than leaving a loading skeleton.

A claim from Pass 2 worth correcting: the 4.2 MB `sentinel.db-wal` noted at session
start is not a leak. It is now 766 KB with the same processes running, so WAL
checkpointing works and no fix was needed. No change was made for it.

### Leaked credential — closed out

The bot token committed in `CLAUDE.md` was revoked by the operator, and history has
now been rewritten with `git filter-branch` and force-pushed. Verified: no reachable
commit on any of the six remote branches contains it; `refs/original`, the stale
backup branches and the `pre-token-purge` tag were deleted and the objects garbage
collected locally. 81 commits and 63 tracked files survive intact, and the 109 tests
pass on the rewritten tree. GitHub can still serve an orphaned commit by direct SHA
until its own GC runs, which is why revocation was the fix that mattered.

### Still open

`ISSUES.md` unchanged: four items, each needing an operator decision.

**Confidence that `gmgn/` is production-ready: high.** But Pass 3 was **not** a clean
pass — it found seven issues, three of them reachable from outside the machine. The
stopping condition is two consecutive passes with nothing new worth fixing, so at
minimum Pass 4 and Pass 5 remain.

## Pass 4 — 2026-07-26

Re-read with fresh eyes: `supervisor.py`, `tunnel.py`, `config.py`, `paper_engine.py`
(stats and refresh paths), `webapp.py` (auth configuration).

### Found and fixed

| # | Severity | Finding | Fix | Commit |
|---|---|---|---|---|
| 41 | High | `_parse_env_file` read one physical line per assignment, so the PEM-format `GMGN_PRIVATE_KEY` was truncated to `"-----BEGIN PRIVATE KEY-----`. `--import-gmgn` copied that truncation into the project `.env`, where it looked like a credential. The real key is 118 chars over three lines; the project held 28 | Quoted values may span lines; `_drop_keys` removes continuation lines with their key; `.env` re-imported and verified byte-identical | `ee4cfd8` |
| 42 | High | Two tunnel bugs cancelling out: a failed probe left the dead hostname in `tunnel.url`, and `main()` gated `watch()` on `tunnel.url` being set. Reconnection only ran because the field held a lie — fixing either alone would have left the Mini App local-only with nothing retrying | Failed probe clears the URL; `watch()` runs whenever a tunnel was requested | `2edf8d3` |
| 43 | Medium | Auth defaulted on only for a public URL. Binding to `0.0.0.0` exposes the same data to the LAN and defaulted auth off | Auth follows reachability; `serve()` refuses an unauthenticated non-loopback bind unless set explicitly | `2edf8d3` |
| 44 | Medium | `refresh_wallet_stats` matched neither branch when the API answered without a usable win rate, so `updated_at` never moved and the row sat at the head of the `ORDER BY updated_at` queue forever | Moves to the back, inventing nothing | `ee4cfd8` |
| 45 | Medium | `get_stats` trusted the address key in a response, which `learn_new_makers` inserts directly | Validated | `ee4cfd8` |
| 46 | Low | `config.apply_to_environ`, `config.gmgn_credentials_present`, `paper_engine.BUDGET` had no callers | Removed | `ee4cfd8` |
| 47 | Low | `summary()` masked a hand-written list of three keys, so a newly added credential could print in full | Masking driven by `is_secret()` | `ee4cfd8` |
| 48 | Low | `Child.stop()` and `Tunnel.stop()` called `kill()` without reaping | `wait()` after `kill()` | `2edf8d3` |

### Verification

```
$ python -m unittest test_paper_engine
Ran 121 tests in 9.225s
OK
```

The credential corruption (#41) was found by reading `config.py`'s own masked output
— `"---...(28 chars)` — not by reading the parser. A leading quote inside a value and
a 28-character private key are both impossible. After the fix the project `.env`
round-trips byte-identically against the machine-wide source, the Telegram token and
API key are untouched, a live feed call still succeeds, and `gmgn_env()` carries the
full 118-character key.

Nothing observable had broken: every endpoint this engine uses is read-only, and the
README states the runtime never needs a private key. It was a corrupt credential
sitting in the file looking valid — which is the kind of thing that fails much later,
somewhere else.

The paired tunnel bugs (#42) were verified on a live run where the first URL failed:

```
WARN  https://drrap-...free.pinggy.net never served a request
      ([SSL: DECRYPTION_FAILED_OR_BAD_RECORD_MAC]) — treating as failed
WARN  tunnel unavailable — the Mini App stays local-only
WARN  tunnel dropped — reconnecting
INFO  Mini App published at https://lcque-...free.pinggy.net
```

The bind guard (#43) was checked directly: `--host 0.0.0.0` without auth exits with
`refusing to serve on 0.0.0.0 without authentication`, while loopback is unaffected.

### Still open

`ISSUES.md` unchanged.

**Confidence that `gmgn/` is production-ready: high.** Pass 4 was **not** clean — eight
findings, two of them high severity, and one was a bug in code written during this
work rather than inherited. That is the argument for the stopping condition: four
passes in, a fresh read still turns up a corrupt credential. Pass 5 remains.

## Pass 5 — 2026-07-26

A different lens: instead of re-reading, cross-check the code against its own
documentation, and check accounting properties against the live database.

### Found and fixed

| # | Severity | Finding | Fix | Commit |
|---|---|---|---|---|
| 49 | Medium | The code reads 30 environment variables; `.env.example` documented 19. Most of the missing ones were added during this hardening work — a knob nobody can discover is a knob that does not exist | All documented, grouped by effect, with real defaults; three tests now enforce it | `3787717` |

### Verification

```
$ python -m unittest test_paper_engine
Ran 127 tests in 9.246s
OK
```

Four accounting invariants were checked against the live database and all held:

| Invariant | Result |
|---|---|
| `balance + open stakes == initial + realised` | 0.03886266 vs 0.03886266, diff 1.4e-17 |
| `entries − exits == open positions` | 12 − 11 = 1, open 1 |
| every open position's latest trade is its `ENTRY` | no offenders |
| no negative balance, no non-positive or inverted prices | 0 offenders |

They are now pinned by `AccountingInvariantTests`, which drives a synthetic sequence
through entry, hard stop, re-entry, trailing stop and max-hold expiry, asserting the
invariants after each step.

Two tests written this pass were themselves verified by deliberately breaking the
code and confirming they fail:

- Changing the exit payout to `stake + pnl*0.9` produced
  `money is not conserved after winning exit`.
- Changing a documented default produced
  `GMGN_CLI_TIMEOUT: example says '999', code uses '45'`.

Both were reverted and the working tree confirmed clean against git.

Writing the documentation test also caught a bug in its own first version: the regex
stopped at the first comma, so a quoted default containing one
(`TUNNEL_PROTOCOLS="quic,http2"`) was compared as `"quic"`.

### Still open

`ISSUES.md` unchanged.

**Confidence that `gmgn/` is production-ready: high.** Pass 5 found one issue, so it
was not clean either. The stopping condition needs two consecutive passes with
nothing worth fixing; Pass 6 follows.

## Pass 6 — 2026-07-26

Lens: failure injection, adversarial input, and checking the documentation's factual
claims rather than re-reading the code.

### Found and fixed

| # | Severity | Finding | Fix | Commit |
|---|---|---|---|---|
| 50 | High | README states the runtime has "no private key, signing, swap submission". `gmgn_env()` had been injecting `GMGN_PRIVATE_KEY` into every `gmgn-cli` subprocess since `config.py` was introduced — a capability a paper engine has no business holding, and a regression introduced by this work | Signing key stripped by default, including one inherited from the ambient environment; `allow_signing=True` is the reviewable opt-in, and a test asserts no file uses it | `cfaddae` |

Every call the engine makes was verified to work with the key withheld before the
change was made: smart-money feed, portfolio stats, token info, KOL discovery. Live
afterwards: key absent from the subprocess environment, API key present, feed returns
5 rows, token price 0.0012236521.

### Checked and found sound — recorded, not changed

**Adversarial input to the JSON API.** `limit` = `0`, `-5`, `99999999`, `abc`, empty,
`1e400`, duplicated, `limit[]`, NUL byte, `' OR 1=1--`; paths with traversal, NUL
bytes, case changes and a 3 KB query string. Every response was either 200 with the
limit correctly clamped or 404 — no 500s, no tracebacks in the server log. Clamping
was verified by row count, not by status code: `limit=0` returns 1 row, `limit=99999`
returns 500 wallets and 300 events against caps of 500 and 300.

**Failure injection.** A missing database yields 503 with no detail leaked, while
`/api/health` and the page still serve. A `gmgn-cli` that exits non-zero leaves the
cycle running, the position tracked, the heartbeat written, and raises the STUCK
alert.

**Fifteen minutes of continuous running.** 40 cycles, mean 7.9 s, slowest 9.4 s
against a 15 s poll. Zero tracebacks, zero crash restarts, two intentional restarts
(the tunnel republish). Memory flat at 17–25 MB per process. The only warnings are
the expected tunnel-probe rejection sequence.

**Stdlib-only claim.** An AST sweep of `gmgn/` finds no third-party import, as
`CLAUDE.md` states.

### Verification

```
$ python -m unittest test_paper_engine
Ran 131 tests in 9.246s
OK
```

### Still open

`ISSUES.md` unchanged.

**Confidence that `gmgn/` is production-ready: high.** Pass 6 was not clean — one
high-severity finding, again a regression from this work rather than an inherited
bug. Two of the last three high-severity findings have been self-inflicted, which is
worth stating plainly: the passes are catching my own changes as much as the original
code. Pass 7 follows.

## Pass 7 — 2026-07-26

Lens: audit the tests themselves, and confirm the panel renders real data end to end.

### Found and fixed

| # | Severity | Finding | Fix | Commit |
|---|---|---|---|---|
| 51 | Medium | An AST sweep of the suite for tests whose only assertion is "result is empty" found two that could pass for the wrong reason: `test_non_pump_launchpad_is_ignored` and `test_first_run_does_not_replay_the_journal`. A broken fixture produces the same empty result as the behaviour under test | Positive controls added to both | this pass |

This is the same failure mode caught in Pass 2, where tightening address validation
silently turned working tests into vacuous ones. The scan is worth keeping in mind:
of 131 tests, 8 assert an empty result, and 6 of those are legitimate (documentation
checks assert an empty list of violations, which is the point).

The controls were verified to bite. Breaking `allowed()` to `return False` — so every
cluster comes back empty — now fails `test_non_pump_launchpad_is_ignored`, which
would previously have passed. All five `ClusterTests` fail, as they should. Reverted,
working tree confirmed clean against git.

### Checked and found sound

The Mini App was loaded in a browser against the live database and renders in full:
open position at −31.11% with entry, current, peak, stop and a 5h 8m expiry
countdown; 11 closed trades; the wallet pool including the parked count added in
Pass 4; and the engine parameters. Not an inference from a 200 response.

### Verification

```
$ python -m unittest test_paper_engine
Ran 131 tests in 9.592s
OK
```

Stack healthy after the Pass 6 change: tunnel published, auth required, Mini App
button installed, cycles at 7.9 s.

### Still open

`ISSUES.md` item 2 updated with current figures, because the situation has moved:
equity 0.0311 SOL of an initial 0.1, and free balance 0.0139 against a 0.025 stake.
Once the open position closes the engine will have too little to open another and
will idle. That is a decision point, not a defect.

**Confidence that `gmgn/` is production-ready: high.** Pass 7 found one issue, so it
was not clean. Pass 8 follows.

## Pass 8 — 2026-07-26

Lens: consistency *between* modules — rules implemented more than once.

### Found and fixed

| # | Severity | Finding | Fix | Commit |
|---|---|---|---|---|
| 52 | Medium | The stop level was computed independently in `exits()`, the Mini App and the bot's `/positions`. They agreed, but nothing made them agree: a change to `exits()` would have left both surfaces displaying a stop the engine no longer enforced | `pe.stop_level()` — one definition, used to decide and to display | `47fcf47` |
| 53 | Low | "Is the engine alive" was two copies of `max(120, POLL*6)` | `pe.engine_is_alive()`, plus `GMGN_ALIVE_GRACE_SECONDS` | `47fcf47` |
| 54 | Low | Win-rate buckets in the panel and `/wallets` hardcoded 0.90/0.70/0.60/0.50, mirroring `weight()` by hand | `WEIGHT_TIERS` is the ladder `weight()` applies and the buckets are built from it | `47fcf47` |

Showing a stop that is not the stop is worse than showing nothing, which is why a
triplicated rule counts as a defect here rather than a style preference.

### Verification

```
$ python -m unittest test_paper_engine
Ran 135 tests in 10.164s
OK
```

The new agreement test was checked by making `exits()` close 5% away from the level
it reports; `test_exits_closes_exactly_at_the_reported_level` then failed on every
scenario. Reverted, and the remaining diff confirmed to contain only the refactor.

Live cross-check against the real open position: engine, panel and bot all report a
stop of `1.839089e-06`. Wallet buckets agree too — the panel's `w90=157` plus
`w70=163` is the bot's `70%+ 320`.

The documentation test from Pass 5 earned its keep unprompted: adding
`GMGN_ALIVE_GRACE_SECONDS` failed `test_every_tunable_is_documented` until it was
written into `.env.example`.

### Still open

`ISSUES.md` unchanged.

**Confidence that `gmgn/` is production-ready: high.** Pass 8 found three issues, so
it was not clean. Pass 9 follows.

## Pass 9 — 2026-07-26 — **clean**

Lens: the surfaces not yet stressed — the import graph after Pass 8 added
cross-imports, concurrency, multi-process contention, repository hygiene, and the
`CLAUDE.md` reference tables.

### Nothing found worth fixing

| Checked | Result |
|---|---|
| Import graph after `webapp` and `telegram_bot` began importing `paper_engine` | All nine modules import standalone; no cycles. Import costs 85 ms, spawns nothing, opens no database |
| 200 concurrent requests across all endpoints, price cache rebinding every second | 200/200 HTTP 200 in 1.0 s, no empty bodies, no server-side traceback |
| Three engines running 25 cycles each against one database | No errors — WAL plus the busy timeout absorbed it — and money still conserved |
| Anything sensitive tracked by git | None. The only secret-shaped match is a docstring describing the bug that was fixed |
| Working tree and untracked files | Clean |
| `CLAUDE.md` bot-command table vs what is registered and answered | Exact match, both directions |
| `CLAUDE.md` parameter table vs resolved config | No mismatches |
| Live stack end to end | Page 200, `/api/health` 200, `/api/overview` 401 through the public origin; `/status` reports `🟢 LIVE`, last cycle 24 s ago |

### One cosmetic change, excluded from the count

The suite printed `warning: ... unterminated quote for BROKEN` — emitted by the very
test that exercises that path. Its stderr is now captured and asserted on, so a
passing run is silent and a genuine warning would stand out. The directive excludes
cosmetic nitpicks from the stopping condition, and this is recorded as one rather
than quietly counted as a finding.

```
$ python -m unittest discover -s gmgn -p 'test_*.py'
Ran 135 tests in 9.645s
OK
```

**This is the first clean pass.** One more is needed.

## Pass 10 — 2026-07-26

Lens: re-read the two most-changed files fresh, then sweep systematically for any
literal that mirrors a named value.

### Found and fixed

| # | Severity | Finding | Fix | Commit |
|---|---|---|---|---|
| 55 | Medium | Pass 8's unification was incomplete. `WEIGHT_TIERS` became the source of truth and the panel and bot were rebuilt from it, but four hardcoded copies were left inside `paper_engine` itself — `cached_weights` filtered on `winrate>=0.50` in SQL, `cleanup_wallets` blacklisted below a literal `0.50`, and two call-out gates compared against `.70` | All four derive from `MIN_WEIGHTED_WINRATE` / `TOP_WINRATE` | `ca74456` |

`cached_weights` is the one that mattered: raising the bottom tier would have left the
SQL admitting wallets that then scored zero, so the dict would carry weight-0 entries
that `enter()` sums into a score — the reported score and the wallet count behind it
would have drifted apart.

Verified by moving the ladder rather than by inspection. With the bottom tier at 0.65,
a wallet at 0.55 is no longer admitted, one at 0.90 still is, and every returned weight
is non-zero. Reverted, tier line confirmed intact.

### Swept and found clean

A scan for numeric literals equal to any named threshold returned only false positives,
recorded rather than "fixed":

- `config.py` — the literal *is* the default being defined, not a copy of it.
- `mass_discovery.py --min-winrate 0.50` — numerically equal to the bottom weight tier
  but a different concept: that tool's own quality gate, which should stay independent
  of the engine's ladder.
- `time.sleep(0.25)` in two rate-limit paths — coincidentally equal to `ENTRY_SCORE`.

```
$ python -m unittest discover -s gmgn -p 'test_*.py'
Ran 137 tests in 9.606s
OK
```

**Pass 10 was not clean**, so the run of consecutive clean passes resets to zero.
Pass 9 was clean; Pass 10 was not; two more are needed. Notably this finding was an
incomplete fix from Pass 8 — the passes keep catching this work rather than the
original code, which is itself the argument for continuing.

## Pass 11 — 2026-07-26

Lens: an AST sweep of every `except` handler for ones that neither log nor act.

### Found and fixed

| # | Severity | Finding | Fix | Commit |
|---|---|---|---|---|
| 56 | Medium | The `"winrate" in key` substring bug — False for `"win_rate"` — was fixed in `paper_engine` and `mass_discovery` in Pass 2 but survived in `monitor.py`, which has its own `number()`. `qualifies()` compares against `MIN_WINRATE=0.70`, so a wallet reporting 75 under that spelling parsed as 75.0, cleared the gate for the wrong reason, and was written to `wallet_scores` as qualifying | Shares `_is_winrate_key` with the engine; the test now iterates all three parsers | `c772ce7` |

`number()` also swallowed a parse failure with `pass`, abandoning the whole key lookup
instead of trying the remaining spellings; it now continues.

The other seven handlers the sweep flagged are deliberate and were left alone: stream
reconfiguration that may legitimately fail, a `KeyboardInterrupt` with cleanup in
`finally`, `kill()` followed by a nested wait, and a pre-heartbeat database fallback.

```
$ python -m unittest discover -s gmgn -p 'test_*.py'
Ran 137 tests in 9.626s
OK
```

Stack healthy throughout: cycles at 5.5–7.6 s against a 15 s poll.

### Where this is heading

Findings per pass: 21, 12, 7, 8, 1, 1, 1, 3, **0**, 1, 1. The trend is real, and so is
the pattern in what is left — the last four findings were all the *same* kind: a fix
applied in one place and not propagated to the others that shared the rule. Pass 8
unified the stop level but left literals inside `paper_engine`; Pass 2 fixed win-rate
scaling in two of three parsers.

Each such fix now ends with a test that enumerates every implementation rather than
checking one, which is what stops that class recurring. That is why the remaining
findings are worth the passes, but it is also fair to say the returns have narrowed
to this one seam.

**Pass 11 was not clean.** Two consecutive clean passes are still required; the run
currently stands at zero.
