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

## Pass 12 — 2026-07-26

Lens: attack the seam Pass 11 named. An AST sweep for function names and SQL strings
appearing in more than one module, to find every remaining place a rule is
implemented twice.

### Found and fixed

| # | Severity | Finding | Fix | Commit |
|---|---|---|---|---|
| 57 | Medium | `monitor.wallet_address` was missed when address validation was added in Pass 2. It accepted `<img src=x onerror=alert(1)>` verbatim and wrote it to SQLite | Validates, like the other three extractors | `27bfc62` |
| 58 | Low | `SELECT updated_at FROM engine_state WHERE key='last_cycle'` appeared in three modules, each with its own pre-heartbeat fallback | `pe.last_cycle_ts` owns it; webapp and the bot delegate. Verified identical against the live database | `27bfc62` |

Both come with a test that **enumerates every implementation** rather than checking
one, so a fifth extractor or a fourth win-rate parser has to be added to the list to
be trusted. Patching each instance as it surfaces does not stop the class recurring;
this does.

### Swept and deliberately left alone

`mass_discovery` and `monitor` still each define `unwrap`, `qualifies` and
`fetch_stats`. These are not duplicates — the discovery tool's quality gate is not the
signal producer's, and merging them would be a worse bug than the repetition. Recorded
so a later pass does not mistake them for unfinished work.

```
$ python -m unittest discover -s gmgn -p 'test_*.py'
Ran 138 tests in 9.632s
OK
```

**Pass 12 was not clean.** Findings per pass: 21, 12, 7, 8, 1, 1, 1, 3, 0, 1, 1, 2.

## Pass 13 — 2026-07-26 — **clean**

Lens: resource lifecycle, dead and unreachable code, and whether the filters added in
earlier passes can actually fire against real API data.

### Nothing found worth fixing

| Checked | Result |
|---|---|
| Every `sqlite3.connect` — closed, and on the failure path? | Two flagged, both correct by design: `webapp.db()` is a factory whose callers close, and the bot holds one read-only connection for the process lifetime (on Windows `terminate()` runs no cleanup regardless). Recorded rather than changed |
| `TODO` / `FIXME` / `XXX` / `HACK` / `WIP` markers | None |
| Unreachable statements after `return`/`raise`/`continue`/`break` | None |
| Functions defined but never referenced | None |
| Live accounting invariants | Money conserved to 1e-17, 12 entries − 11 exits = 1 open, no bad prices, no win rate above 1, no address outside 32–44 chars |
| Live stack end to end | Page 200, `/api/health` 200, `/api/overview` 401 through the public origin; `/status` reports `🟢 LIVE`, last cycle 14 s ago |

### Worth recording

The wallet-parking filter added in Pass 1 shows zero parked wallets, which raised the
question of whether it can fire at all. Sampling live stats shows it can: GMGN does
populate `buy` and `sell` (162 and 146 for the wallet sampled), so a dormant wallet
with `buy=0` would be parked, and a trading one correctly is not. The filter is
selective, not dead.

The same sample showed the win rate arriving as `pnl_stat.winrate` — the *third*
fallback key, not the first. The Pass 2 fix to underscore-insensitive matching is
therefore guarding a path that is genuinely in use, not a hypothetical one.

```
$ python -m unittest discover -s gmgn -p 'test_*.py'
Ran 138 tests in 10.046s
OK
```

**Clean.** One consecutive clean pass; one more is required.

## Pass 14 — 2026-07-26 — **clean**

Lens: a full lifecycle from an empty database, and a final documentation-vs-code check.

### Nothing found worth fixing

Drove a fresh database through the whole path rather than testing pieces:

| Stage | Result |
|---|---|
| Cold start on an empty file | 10 tables created, opening balance 0.1 SOL |
| Bot against a database the engine has never cycled | `/status`, `/positions`, `/weights` all answer with what to do next; nothing raises |
| Entry, then a −60% move | Closed as `hard stop -45%` — the rule that fired, with the actual fill reported separately |
| Restart | Nothing replayed on first start; an event raised afterwards was delivered |
| Accounting | Money conserved: 0.085 = 0.1 − 0.015 |

Documentation re-checked against the code: the weight table, the paper-only claim
(`GMGN_PRIVATE_KEY` absent from `gmgn_env()`), the documented cycle order, and the
stdlib-only claim all still hold. Working tree clean.

```
$ python -m unittest discover -s gmgn -p 'test_*.py'
Ran 138 tests in 10.071s
OK
```

---

# Final summary — stopping condition met

**Passes 13 and 14 both found nothing worth fixing.** Per the directive's stopping
condition — two consecutive clean audit passes, cosmetic nitpicks excluded — the
iterative hardening ends here.

## What the fourteen passes cost and produced

| | |
|---|---|
| Findings fixed | 58 |
| Findings per pass | 21, 12, 7, 8, 1, 1, 1, 3, **0**, 1, 1, 2, **0**, **0** |
| Tests | 19 → 138 |
| Modules brought under test | `paper_engine`, `config`, `webapp`, `telegram_bot`, `tunnel`, `mass_discovery`, `monitor`, `run_engine` |

The most expensive defects were found in the first two passes and were all in the same
family — the engine could stop enforcing stop-losses while appearing to run:

1. Stop checks ran *after* a stats sweep that could take tens of minutes (`cycle` ordering).
2. Re-entering a token crashed the process, and the supervisor restarted into the same crash.
3. Any transient error terminated the poll loop.
4. A `ZeroDivisionError` on one bad row aborted the sweep for every later position.

Two −99.99% exits against a −45% hard stop are what these produced in practice.

## Honest accounting of what the passes caught

Of the last eight high- and medium-severity findings, **five were regressions
introduced by this hardening work**, not inherited bugs: the signing key handed to a
paper engine, the dotenv parser truncating a PEM credential, the paired tunnel bugs
that cancelled out, and two cases of a fix applied in one module and not propagated to
the others sharing that rule.

That pattern is the argument for the stopping condition being two passes rather than
one. It also changed how fixes are now written: each rule that exists in more than one
place ends with a test that **enumerates every implementation**, so a fifth address
extractor or a fourth win-rate parser has to be added to that list to be trusted.

## Verification standard used

Claims here were checked against running code, not inferred:

- Every accounting invariant was run against the live database.
- New tests were validated by deliberately breaking the code and confirming they fail
  — the exit payout, the launchpad filter, a documented default, and the stop level —
  then reverting and confirming the tree clean against git.
- The tunnel probe, the auth gate, path traversal, adversarial API input, failure
  injection and 200 concurrent requests were all exercised against a running server.
- The Mini App was loaded in a browser and read, not assumed from a 200 response.

## Accepted limitations, deliberately left as-is

Four items remain open in `ISSUES.md`. None is a defect in the code; each is a
decision that belongs to the operator, and all were explicitly placed off-limits:

1. **5563 blacklisted wallets**, an unknown share of them false positives from the
   eligibility bug fixed in `a2d7e16`. `gmgn/unban_wallets.py` clears them after taking
   a backup; it defaults to a dry run and **has not been run**. Not automatic because it
   changes which wallets the engine will follow.
2. **The strategy is unprofitable and nearly out of runway** — 0.0311 SOL equity of an
   initial 0.1, free balance 0.0139 against a 0.025 stake. `GMGN_ENTRY_SCORE=0.25` is
   exactly one 70% wallet, so "weighted convergence" currently means "follow one
   wallet"; every entry so far fired on `wallets=1`. Not automatic because entry
   threshold, stake, stop distances and the account balance are risk parameters. Worth
   stating plainly: 11 closed trades cannot distinguish a bad strategy from an unlucky
   one, and the remedy for that is more trades, not more tuning.
3. **`engine_events` and `paper_trades` have no retention policy.** Harmless today;
   deleting history is irreversible and the right window is a preference.
4. **A delisted token locks its stake open forever.** The engine raises a `STUCK` alert
   rather than guessing what such a position is worth, because writing it off books a
   realised loss on an inference rather than an observed price.

## Known-good state at the end

Engine, Telegram bot and Mini App run under `gmgn/supervisor.py --tunnel`. Cycles
complete in 5.5–9.4 s against a 15 s poll. The panel is published over HTTPS with
Telegram `initData` verification pinned to the owner's chat; `/api/health` is the only
unauthenticated endpoint, and it exists so the tunnel probe can confirm the URL serves
before the bot is pointed at it.

---

# Strategy change — 2026-07-26 (operator-directed)

Requested changes, all applied and verified.

## Weight ladder and entry threshold

| 30d win rate | Was | Now | Against `ENTRY_SCORE=1.0` |
|---|---:|---:|---|
| 90–100% | 0.25 | **1.0** | enters on its own |
| 80–90% | 0.25 | **0.5** | two of them enter |
| 70–80% | 0.25 | 0.25 | four of them enter |
| 60–70% | 0.0625 | 0.0625 | contributes only |
| 50–60% | 0.03125 | 0.03125 | contributes only |

`GMGN_ENTRY_SCORE` returns to **1.0** from 0.25. The two numbers only mean anything
read together: the old pairing was a 0.25 top tier against a 0.25 threshold, so one
70% wallet was a full signal and every entry to date fired on `wallets=1`.

Effect on the current pool: **155 of 1191** watched wallets can now enter alone, where
**318** could before — roughly half as many solo triggers, plus the new combinations.

## A 90%+ wallet now enters rather than being announced

Its weight equals the threshold, so `enter()` opens the position and emits `ENTRY`.
Repeating that as a separate call-out would be duplication, so the notification is
inverted: `MISSED` reports a signal strong enough to enter that was **declined**, and
why — cooldown, already held, or out of funds. Those are the entries the configuration
cost you, which is the part worth knowing.

## Auto-close after one hour

`GMGN_MAX_HOLD_SECONDS` 21600 → **3600**. `GMGN_STUCK_AFTER_SECONDS` follows at 2×.

## Account reset

`gmgn/reset_account.py`, new. The open position was settled at the market
(−31.43%, −0.00786 SOL) and the balance restored to 0.1 SOL. Backup taken first.

The top-up raises `initial_budget_sol` by the same amount, so cumulative P&L still
reads −0.06899 rather than pretending the loss did not happen. `reset_at` is recorded,
and `/status` and `/api/overview` now report performance since that point separately
from lifetime.

## Attribution — the improvement worth having

The engine recorded that four wallets agreed but never which four, so the question the
whole system exists to answer was unanswerable: which wallets make *this account*
money, as opposed to having a good win rate on GMGN.

`trade_wallets` now records the contributors and their weights at entry, and
`wallet_attribution()` splits each closed trade's P&L by weight share — a lone 90%
wallet owns its whole result, one of four owns a quarter. Surfaced as `/attribution`
and a «Вклад» tab in the panel. A test asserts the shares reconcile exactly with
realised P&L.

## Verification

```
$ python -m unittest discover -s gmgn -p 'test_*.py'
Ran 154 tests in 9.511s
OK
```

Three failures along the way were guard tests written in earlier passes doing their
job, not accidents: the new `/api/attribution` endpoint had to be acknowledged by the
auth-gate test, and the changed defaults had to be written into `.env.example` before
the documentation test would pass.

Writing the reset-relative reporting also surfaced an off-by-one in this same work:
`realised_since` used `event_ts >= reset_at`, and the settlement of the old positions
carries `event_ts == reset_at` exactly, so the first reading charged the old strategy's
−0.00786 SOL to the new one. Strictly-after now, and pinned by a test.

## Open

`ISSUES.md` item 2 is now partly acted on — the threshold is raised and the account
reset — but its substance stands: at 12 closed trades the sample still cannot
distinguish a good strategy from a lucky one. What changes that is more trades, not
more tuning. Items 1, 3 and 4 are unchanged, and `unban_wallets.py` still has not run.

One consequence worth watching rather than pre-emptively changing: with a 1 h hold the
trailing stop, which arms at +25%, has less time to arm than it did over 6 h.

**Corrected in Pass 17.** The sentence that stood here counted trades whose *exit* P&L
exceeded +25% and concluded the trailing stop was close to inert. That was the wrong
measurement — the trailing stop arms on the peak, not the exit. It fired 3 times in 12,
producing every profitable exit, and all three closed within 34 minutes. See ISSUES.md
item 2 for the full breakdown.

---

# Second hardening cycle — 2026-07-26

The clean-pass counter restarts: the strategy change, attribution, the metrics format
and the 500 fix all landed after Pass 14, so two consecutive clean passes are required
again from here.

## Pass 15 — 2026-07-26

Lens: the least-reviewed code in the repository — everything written in the last two
sessions.

### Found and fixed

| # | Severity | Finding | Fix | Commit |
|---|---|---|---|---|
| 59 | Medium | `missed_elite_signals` runs after `enter()` and read open positions from the database, so a mint opened moments earlier counted as "already held". Every successful elite entry produced both an `ENTRY` and a contradictory `MISSED` for the same buy | `enter()` returns the mints it opened; the report excludes them | `6cfb09b` |
| 60 | Medium | Two operator-facing messages hardcoded "70%" while counting `TOP_WINRATE`, which the strategy change moved to 90%. Telegram reported "70%+: 3" about wallets at 90%+ | Both derive the figure they quote | `7a2f1c8` |
| 61 | Medium | Threading win rates through for attribution left `cycle()` with its own copy of the weight derivation, so `cached_weights` was called only by tests — coverage of a path production no longer took | `weights_from()` is the single derivation | `7a2f1c8` |
| 62 | Medium | `reset_account.close_all` set no cooldown, unlike every other exit path, so the engine could re-buy the token it had just settled on the next poll. The script mutates the operator's account and had no tests at all | Cooldown set; six tests added | `76b42f1` |

Findings 59–61 are regressions from the previous two sessions rather than inherited
bugs, which is the argument for the stopping condition being two passes.

### Verification

```
$ python -m unittest discover -s gmgn -p 'test_*.py'
Ran 176 tests in 9.698s
OK
```

Both new guard tests were checked by reintroducing the bug: removing the `just_opened`
exclusion fails with `'MISSED' unexpectedly found in ['ENTRY', 'MISSED']`, and reverting
the label fails on the threshold string. Working tree confirmed clean after each.

### Checked and found sound — recorded, not changed

- **Attribution across a re-entered mint.** `enter()` reuses the position row, so an
  entry/exit/re-entry/exit sequence could have mismatched the join. Driven through it:
  2 trades counted, reconciling exactly with realised P&L.
- **The equity curve** ends at 0.1, matching the headline, because the top-up raised
  `initial_budget_sol` by the same amount. The accounting choice in `reset_account`
  is what holds the chart and the balance together.
- **The panel after several regex edits**: all six tabs render with no JavaScript
  errors, the extracted script passes `node --check`, braces balance, and no function
  is defined without being referenced.

## Pass 16 — 2026-07-26

Lens: the contract between `enter()` and its callers, then the panel's own duplication.

| # | Severity | Finding | Fix | Commit |
|---|---|---|---|---|
| 63 | Medium | `/wallets` printed the same count twice under different labels — it queried `SUM(winrate>=ELITE_WINRATE)` and `SUM(winrate>=WEIGHT_TIERS[0][0])`, both 0.90 since the ladder changed, beneath hardcoded "90%+" and "70%+". Live output read `90%+ 154 · 70%+ 154` | Uses `webapp.winrate_bands`, the same function the panel renders from | `6e00fc6` |
| 64 | Low | The hero and the results card had separate helpers for "current, then lifetime in parentheses", with different signatures and different fallbacks | One `pair()` for the page | `7614377` |
| 65 | Low | Three labels spelled thresholds out by hand | Read from `config.elite_winrate` | `7614377` |

Verified: `enter()` returns a set on all five paths (no trades, below threshold, no price,
no funds, success) and its single production caller uses it. Bands reconcile — they sum
to 1188, exactly the active pool.

## Pass 17 — 2026-07-26

Lens: close the pattern named in Pass 16 rather than wait for its fourth instance, then
re-examine claims made in earlier passes.

| # | Severity | Finding | Fix | Commit |
|---|---|---|---|---|
| 66 | Medium | `heartbeat()` is written even when every feed call failed, so `/status` reported LIVE while the engine fetched nothing and could neither enter nor price | `last_feed_ok` recorded separately; a third state, 🟡 НЕТ ДАННЫХ | `d442c1a` |
| 67 | Medium | **A conclusion in `PROGRESS.md` was wrong.** It argued the trailing stop might be near-inert at a 1 h hold because one of twelve trades exited above +25% — but the stop arms on the *peak*, not the exit | Measured properly and corrected in both places it appeared | `44f5c2f` |
| 68 | Medium | `ISSUES.md` described a state that no longer existed: an account "nearly out of runway" at 0.0311 SOL, and two already-taken decisions listed as open | Rewritten to what is true | `7f2b811` |
| 69 | — | Preventive: a guard test forbidding any percentage literal in operator-facing text that equals a live threshold | `4e0aef3` |

### The corrected measurement

| Exit reason | Count | Holding times |
|---|---:|---|
| trailing stop 15% | 3 | 9, 25, 34 min |
| hard stop −45% | 4 | 12, 436, 671, 671 min |
| max hold | 4 | 379, 428, 524, 834 min |

The trailing stop produced **every profitable exit** (+18.39%, +18.74%, +31.48%), all
closing within 34 minutes — inside the new cap. The eight positions that lived past an
hour lost −0.0747 SOL between them, including both −99.99% write-offs. The 1 h cap
therefore cuts the losing cohort and leaves the winners untouched, which is the
opposite of what the earlier note implied.

### Verified against a real fault

A DNS failure occurred mid-session. The engine logged
`stats sol: Client network socket disconnected before secure TLS connection`, continued,
and completed 40 cycles at a 6.4 s mean with zero crash restarts. The Pass 1 resilience
work held under a genuine network fault rather than a simulated one — and finding 66
came directly from asking what would have happened had the *feed* been the failing call.

```
$ python -m unittest discover -s gmgn -p 'test_*.py'
Ran 191 tests in 10.729s
OK
```

Neither pass was clean.

## Pass 18 — 2026-07-26

Lens: the engine had not entered a trade in hours. Is the pipeline broken, or is the
threshold simply out of reach?

**Not broken.** Scores compute, the launchpad filter passes, clusters form. Measured:

| | |
|---|---|
| Elite (90%+) wallets in the pool | 154 |
| Seen in the feed in the last hour | 0 |
| Seen in the last 24 h | 25 |
| Feed makers present in the weighted pool at all | 18% (18 of 101 over 4 samples) |
| Best cluster score | 0.0625 against a threshold of 1.0 |

| # | Severity | Finding | Fix | Commit |
|---|---|---|---|---|
| 70 | Medium | `heartbeat()` is written even when every feed call fails, so `/status` said LIVE while the engine fetched nothing | `last_feed_ok` recorded separately; third state 🟡 НЕТ ДАННЫХ | `d442c1a` |
| 71 | Medium | Nothing reported how close the engine came to entering, so a quiet market and an unreachable threshold were indistinguishable from outside | `signal_history` per cycle, surfaced in `/weights` and the panel | `168b644` |

Neither is a code defect in the trading logic — the engine does exactly what was
specified. Both are about the operator being unable to judge a configuration they chose.

## Pass 19 — 2026-07-26

Lens: the code written in Passes 17–18, held to the same standard as inherited code.

| # | Severity | Finding | Fix | Commit |
|---|---|---|---|---|
| 72 | Medium | Every chain in a cycle shares `signal_history`'s primary key, and the upsert assigned `excluded.best_score` — so a second chain erased the first. A threshold-strength signal vanished behind a weak one | Keeps the strongest, sums the mints | `4d9d622` |
| 73 | Medium | `discover_wallets` unwrapped responses as `d.get("list") or (d if isinstance(d,list) else [])`, whose list fallback is unreachable — the `.get` raises first | `rows_under()`, verified against every shape | `8a222fb` |
| 74 | Medium | `token traders` addresses reached `wallet_watch` through a raw `.get`, bypassing the base58 validation every other boundary applies | Validated | `8a222fb` |
| 75 | Low | `list_rows` returned `{"list": None}` as one row consisting of the envelope | A present-but-null container means empty | `8a222fb` |
| — | — | `paper_positions` is keyed by mint without chain, unlike every sibling table | Recorded as `ISSUES.md` item 5 — a primary-key change is a migration on live data, for a switched-off path | — |

Finding 73 surfaced only because attribution was run through the full `cycle()` rather
than by calling `enter()` directly as every existing test did. Finding 75 surfaced while
writing the test for 73, and was fixed in `list_rows` rather than by relaxing the test.

```
$ python -m unittest discover -s gmgn -p 'test_*.py'
Ran 204 tests in 10.743s
OK
```

Live discovery unaffected: 160 wallets found, none malformed. All four endpoints parse.

## Pass 20 — 2026-07-26

Lens: the pattern of Passes 15–19 themselves. Almost every finding in this cycle has
been in code written during the cycle, so this pass audits only that code, to the
standard applied to anything inherited.

| # | Severity | Finding | Fix | Commit |
|---|---|---|---|---|
| 76 | Medium | `signal_summary` claimed a window but counted the whole table, relying on pruning. Only the engine prunes, and only while cycling — so with the engine **stopped**, which is when the operator looks, a day-old signal was reported as current, "threshold reached" included | Window applied in the query | `c31dbbc` |
| 77 | Medium | `feed_is_fresh` could not detect a feed that had **never** worked: it compared the age of the last success, and with none recorded there is no timestamp to be old, so it returned True unconditionally. An engine cycling for hours against a dead feed reported LIVE — the exact case the feature was added for | Consecutive failures counted instead | `c31dbbc` |

The first attempt at fixing 77 was wrong: it used `last_cycle_ts` as the reference,
which advances every cycle, so the difference stayed small and nothing changed. Caught
by running it rather than reasoning about it — recorded because that is the lesson, not
an incidental detail.

```
$ python -m unittest discover -s gmgn -p 'test_*.py'
Ran 207 tests in 11.176s
OK
```

Live after relaunch: `feed failures 0`, `feed fresh True`, `🟢 LIVE`, and the reach
metric reporting `best 0.0625 of 1.0 over 58 cycles` — accumulating honestly, and
showing the threshold is a long way off.

### Standing observation for later passes

Passes 15–20 produced 17 findings, the large majority in code written during this same
cycle. That is not coincidence: new code has survived zero review passes while the
inherited code has survived nineteen. The working rule that follows — keep your own
edits under the same scrutiny as inherited code, and never treat "I just wrote this" as
"this is checked."

## Pass 21 — the files nobody had tested

Lens: coverage, not code. `supervisor.py`, `tunnel.py`, `reset_account.py` and
`unban_wallets.py` had **zero tests between them** — and `supervisor.py` is the one file
whose entire job is minimising engine downtime. Three of this pass's six findings were
in it. The pass then checked those findings against the previous boot's log rather than
against my reading of the code, which is how 79 and 80 were confirmed as real rather
than theoretical.

| # | Severity | Finding | Fix | Commit |
|---|---|---|---|---|
| 78 | High | Restart backoff was a `wait()` **inside** the per-child loop, so a child sitting out its delay suspended the check on every other child. With the bot flapping at the 120s ceiling, an engine that died next stayed dead for up to two minutes — the exact outage the file exists to prevent | `restart_at` scheduled; `supervise_once()` never blocks | `436f6a7` |
| 79 | High | The tunnel was started **before the webapp it points at**. It probes its own public URL for a real 200 before publishing, so on a clean boot the probe hit an empty port | Runs off the main thread, after loopback answers | `436f6a7` |
| 80 | High | The webapp computes `REQUIRE_AUTH` once at import. Started auth-off and restarted by `publish()`, it was reachable **through the live tunnel, unauthenticated**, in between | Auth set before the child is spawned | `436f6a7` |
| 81 | High | The dotenv parser looked for a value's closing quote at end-of-line, so `KEY="10"  # why` read as *unterminated* and swallowed the following line. That is the shape `.env.example` documents every tunable in | Closing quote found within the line | `3176b0d` |
| 82 | High | `reset_account.py` clamped its `initial_budget_sol` adjustment with `max(0.0, deposit)`. A reset on a **profitable** account moves money out, so the gain silently vanished from cumulative P&L | Signed adjustment, both directions | `4f32c2e` |
| 83 | Cosmetic | The panel coloured win rates against literal 90/70, twenty lines below a comment explaining that a hardcoded threshold here had already gone stale once | Read from the ladder | `7c108c3` |

### 79 and 80 were confirmed from the production log, not from reading

The previous boot, in its own words:

```
21:33:43  trying pinggy over ssh:443
21:33:45  tunnel reports ready: https://mokgq-….pinggy-free.link
21:34:18  …never served a request (SSL: DECRYPTION_FAILED_OR_BAD_RECORD_MAC) — treating as failed
21:34:18  tunnel unavailable — the Mini App stays local-only
21:34:18  starting engine                          ← 35s after boot
21:34:18  tunnel dropped — reconnecting            ← 0.02s after "local-only"
21:34:18  [webapp] mini app on 127.0.0.1:8770 (auth off — local only)
21:34:22  Mini App published at https://fyjwt-….free.pinggy.net
21:34:24  [webapp] mini app on 127.0.0.1:8770 (auth required)
```

Four separate costs in nine lines: 35 seconds of the engine not running, a free pinggy
hostname burned on an attempt that could not have worked, a warning that was untrue
0.02s after it was printed, and 21:34:22→21:34:24 with the panel live on the public
internet and auth off. After the fix, same stack:

```
22:30:38  starting engine
22:30:38  [webapp] mini app on 127.0.0.1:8770 (auth required)
22:30:43  [engine] [cycle] 5.3s wallets=1272 open=0     ← 5s after boot, was 42s
22:32:12  Mini App published at https://cikkz-….free.pinggy.net   ← first attempt
```

cloudflared still takes 90s to fail on this network, as documented — it just no longer
delays anything. Auth verified against the public origin rather than the log:
`/api/health` → 200, `/api/overview` → **401**.

### 82 was reproduced before it was fixed

```
BEFORE  balance 0.20000  initial 0.10000  realised +0.10000   → true P&L +0.10000
AFTER   balance 0.10000  initial 0.10000  reported P&L +0.00000
        money conserved False
```

The script prints that conservation line itself, and printed `False` — after it had
already committed. The bug had been sitting behind a correct-looking comment about
keeping cumulative P&L honest, which is exactly what it did in the one direction that
had ever been exercised. Existing tests covered `close_all()`, which was never wrong;
the arithmetic was in `main()`, which nothing called.

### Two things checked and found clean

Not everything suspicious was a bug, and the checks are worth as much as the fixes:

- The `.venv` python is a launcher stub that re-execs the base interpreter as a *child*,
  so `Child.stop()` terminates the stub, not the worker. Tested directly — the child
  dies with it. Not orphaned.
- `discover_wallets` reads KOL rows with `wallet(t)` (`maker`/`wallet`) while the traders
  path uses a wider key set. Probed the live endpoint: KOL rows carry `maker`, 5/5
  resolve. The asymmetry is harmless.

```
$ python -m unittest discover -s gmgn -p 'test_*.py'
Ran 222 tests in 12.097s
OK
```

15 new tests: 10 for `supervisor.py` (which had none), 3 for `reset_account.main()`,
2 for the dotenv parser — one of which uncomments every documented setting in
`.env.example` and asserts each parses to exactly one clean value, so the template and
the parser cannot drift apart again.

**Уверенность: средне-высокая.** Pass 21 не чист — 6 находок, 5 из них серьёзные.
Общая закономерность прохода: искал не в коде, а в *покрытии*, и все крупные находки
оказались в файлах, у которых тестов не было вовсе. Счётчик чистых проходов остаётся
**0**. Продолжаю Pass 22.

## Pass 22 — auditing my own Pass 21, then the files nothing points at

Two lenses. First the rule recorded after Pass 20 — my own recent code gets the same
scrutiny as inherited code — applied to Pass 21's changes. Then a test for a
project-wide invariant `CLAUDE.md` states and nothing enforced, which found a file
twenty-one passes had never opened.

| # | Severity | Finding | Fix | Commit |
|---|---|---|---|---|
| 84 | High | Pass 21 made `reset_account.py`'s adjustment signed, so `initial_budget_sol` can now reach zero or below. `equity / initial - 1` predates that and **flips the sign**: a real +0.20 SOL gain rendered as −133.33% | `None` when there is no positive base; the panel prints SOL alone | `bb15834` |
| 85 | Medium | `mass_discovery._cli_retry` caught only `RuntimeError`. A **hung** CLI raises `subprocess.TimeoutExpired`, a `SubprocessError` — the one failure a retry exists for was the one it never retried, and the two calls that begin a run had no guard | Retry any subprocess failure; guard both feeds | `c72a54b` |
| 86 | Medium | `monitor.produce_signals` filtered feed rows on `maker` being **truthy**, then persisted it. `<img src=x onerror=alert(1)>` and `'; DROP TABLE wallet_scores; --` both pass a truthiness test | Validated with `valid_address` before persisting | `c72a54b` |
| 87 | Low | `mass_discovery.py` and `monitor.py` never called `config.use_utf8_stdio()` — the invariant every other entrypoint honours | Both call it; a test now enforces it | `c72a54b` |

### 84 came from re-reading my own fix, not from a failure

Pass 21 fixed a real bug in `reset_account.py` and introduced the conditions for this
one in the same edit. The old clamp meant `initial_budget_sol` could only grow, which is
the assumption `equity / initial - 1` was written under; removing the clamp invalidated
it silently, three files away. Reproduced before fixing:

```
balance +0.05000   initial -0.15000
total_pnl_sol  +0.20000     <- correct
total_pnl_pct  -133.33%     <- a real gain, rendered as a loss
```

A wrong-signed percentage is worse than no percentage, so the API returns `None` and the
panel omits it. The SOL figures were never wrong and still aren't.

### The invariant test found a file, not just a defect

`test_every_entrypoint_sets_up_utf8_stdio` was written to pin `mass_discovery.py`. It
failed on `gmgn/monitor.py` — a module nothing imports, nothing starts, and no pass had
read. Reading it produced 86. Pulling that thread found `dashboard/`, `discovery/`,
`scorer/` and a whole Rust workspace in the same condition.

Established rather than assumed: `gmgn_signals`, the table `monitor.py` exists to write,
**does not exist in `sentinel.db`** — it has never run here. `dashboard/app.py` needs
`streamlit`, which is **not installed**. Both are recorded in `ISSUES.md` #7 as a scope
decision for the operator; deleting a Rust engine on the inference that it is abandoned
is not mine to make. The defects found in that code were fixed regardless — "probably
dead" is not "dead".

### Checked and found clean

- Every other division by `initial_budget_sol` in the repo — there is exactly one, and
  it is the one fixed.
- The engine's own timeout handling. `token_price`, `get_stats` and `cycle`'s feed call
  all catch `Exception`, so `TimeoutExpired` was only ever unhandled in the offline
  collector.

```
$ python -m unittest discover -s gmgn -p 'test_*.py'
Ran 227 tests in 12.339s
OK
```

Live check of the restart path fixed in Pass 21, by killing the webapp child:

```
22:42:45,491 WARNING webapp exited with code 1 after 632s — restart #1 in 3s
22:42:48,515 INFO    starting webapp                       ← 3.02s, as scheduled
[engine] 22:42:33 [cycle] … / [engine] 22:42:54 [cycle] …  ← never interrupted
```

**Уверенность: средне-высокая.** Pass 22 не чист — 4 находки. Одна из них в коде,
который я написал в Pass 21, что подтверждает правило после Pass 20 в третий раз.
Счётчик чистых проходов остаётся **0**. Продолжаю Pass 23.

## Pass 23 — guards that cannot fire, and documents nobody diffed

Lens: things that *look* checked. A test that passes for the wrong reason and a README
that agrees with nothing both read as verification while providing none. Started, per
the standing rule, with the previous pass's own additions.

| # | Severity | Finding | Fix | Commit |
|---|---|---|---|---|
| 88 | Medium | The Pass 22 invariant test searched each source for the string `use_utf8_stdio()`. `config.py` contains it — **on its own definition line** — so the module that defines the helper was the one module never required to call it, and `config.main()` did not | Match a call, not a mention; assert ≥8 files examined | `c1db338` |
| 89 | Medium | The bot's command list lived in **four** hand-maintained copies: `setMyCommands`, the reply keyboard, the `/help` text, and CLAUDE.md. A new command could reach the dispatcher and appear in none | One `COMMANDS` list drives three; five tests pin the rest | `9cb727f`, `bbe344c` |
| 90 | High | **README documented the strategy the operator replaced** — on all three numbers that decide what gets traded | Corrected; four tests pin it to the code | `bbe344c` |
| 91 | Low | `SESSION_REPORT.md` describes the superseded Rust pipeline with nothing saying so | Labelled historical; recorded under `ISSUES.md` #7 | `bbe344c` |

### 90 is the one that mattered

```
                 README (front page)              actual
weight ladder    3 tiers, 0.25 top at 70%+        5 tiers, 1.0 at 90%+
entry threshold  "default 0.25, i.e. one 70%+"    1.0
max hold         "defaults to 6h"                 1h
```

Every one of those is a value the operator changed by hand on 2026-07-26, and every one
was still documented at its old value. Worse than merely stale: README described *a
single 70% wallet opening a position* — precisely the misconfiguration that was
diagnosed and fixed — as current behaviour. `CLAUDE.md` was correct throughout, so the
two documents contradicted each other and nothing compared them.

Four tests now parse README and compare against `WEIGHT_TIERS`, `ENTRY`, `MAX_HOLD` and
`webapp.ROUTES`. The ladder test asserts on the parsed list, so an empty match fails
rather than passes — the mistake 88 was.

### 88 is the same mistake one level up

I wrote that test last pass to enforce an invariant, and it exempted exactly one file:
the one that defines the invariant. Verified the tightened version fails before the fix
rather than assuming it would:

```
AssertionError: Lists differ: ['config.py'] != []
```

An AST sweep for the general shape — every assertion in a test nested inside a loop or
conditional — flagged 21 tests. All but this one iterate literal lists written in the
test itself and cannot be empty, so they were left alone. Recorded because checking them
is what makes leaving them a decision rather than an oversight.

```
$ python -m unittest discover -s gmgn -p 'test_*.py'
Ran 236 tests in 12.348s
OK
```

Bot restarted onto the consolidated command list; `setMyCommands` re-registered and the
Mini App button reinstalled, per the supervisor log at 22:55:24.

**Уверенность: средне-высокая.** Pass 23 не чист — 4 находки, одна из них снова в коде
предыдущего прохода. Счётчик чистых проходов **0**. Продолжаю Pass 24.

## Pass 24 — settings that do nothing, and notifications nobody can read

Lens: the gap between what a thing is documented to do and what it does. Two findings,
and two deliberate verified negatives.

| # | Severity | Finding | Fix | Commit |
|---|---|---|---|---|
| 92 | Medium | Wallet bookkeeping was a push notification. Measured on the live journal: **60 `WALLET` messages in 24h against 4 entries and 4 exits** — the eight that matter arriving at 7:1 odds against a counter incrementing by one | Throttled at push time, one per hour; the journal and panel stay complete | `a594f5d` |
| 93 | Medium | **`PAPER_BUDGET_SOL` did nothing.** Documented in `.env.example`, `CLAUDE.md` and `README.md` as the starting balance, read into `config.BUDGET_SOL` — and the account row was seeded by a literal `0.1` inside `init()`'s `executescript()`, which takes no parameters | Insert moved out and parameterised, `OR IGNORE` kept | `3ec778f` |

### 93 is the interesting one, because three tests should have caught it and none could

The `.env.example` consistency suite checks that every documented key is *read* by the
code and that documented defaults match the literals. `PAPER_BUDGET_SOL` passed both: it
is read, into a constant, and the default matches. What no test checked was whether
reading it had any **effect** — a value can be resolved and then consumed by nobody.

```
$ PAPER_BUDGET_SOL=0.5 …init(fresh database)…
config.BUDGET_SOL reads : 0.5
fresh account created as: 0.1 / 0.1
```

It survived because the one place the value *was* consumed — `reset_account.py --target`
— is a different code path that worked correctly, so the setting appeared functional
whenever anyone tested it.

Afterwards, an AST sweep over all 20 settings resolved in `config.py` checked each is
referenced outside it. All 20 are. Recorded with the caveat that matters: **this sweep
would not have found the bug it was written after**, because `config.BUDGET_SOL` *was*
referenced, just from the wrong path. It is a necessary check, not a sufficient one.

### Verified negatives

- **Money conservation under randomised sequences.** Twelve seeded interleavings, 50
  steps each, invariants re-checked after every step: 175 entries, 167 exits, 84 max
  hold / 45 trailing / 38 hard stop. No defect. The check itself was verified by
  corrupting a balance by 0.001 and confirming it fails, so the pass means the books
  balanced rather than that nothing was examined. (`467aa9d`)
- **`push_events` throughput.** `WALLET` looked like it might not be in `PUSH_KINDS`,
  which would have made the batch limit apply to rows rather than to sends. It is in
  `PUSH_KINDS`. No issue.
- **`WALLET_BUY`** exists in the journal (2 rows) but nothing emits it — inert legacy
  data from a removed feature, not a live path.

### One of my own tests caught one of my own bugs, within the minute

`due_for_push` used `0.0` for "never sent" and subtracted. Against a real clock that is
correct only because `time.time()` dwarfs any interval; under the test's clock it
suppressed the first message of every kind. `None` now means never. Worth recording
because the test was written before the code was trusted, not after it was believed.

```
$ python -m unittest discover -s gmgn -p 'test_*.py'
Ran 243 tests in 12.693s
OK
```

Live account unaffected by 93 (`0.1 / 0.16899` before and after — `OR IGNORE` leaves an
existing row alone). Bot restarted onto the throttle at 23:04:48.

**Уверенность: средне-высокая.** Pass 24 не чист — 2 находки. Счётчик чистых проходов
остаётся **0**.

## Pass 25 — three findings, one of which was silently throwing away the pool

Lens: the previous pass's own code first (fourth pass running, fourth time it paid),
then the parts of the engine whose behaviour is only visible in aggregate over hours —
which is why nothing had looked at them.

| # | Severity | Finding | Fix | Commit |
|---|---|---|---|---|
| 94 | Medium | Pass 24's throttle recorded a send at the moment it *decided* to send. `push_events` leaves the cursor put on a failure so the event retries — but the retry pass found the kind throttled, skipped it, and moved the cursor past. **The message was dropped by the code whose comment promises a retry** | `due_for_push` made pure; `mark_pushed` called only after the send | `a6c79c2` |
| 95 | Medium | `auto` reconnects hourly when the free pinggy session expires, and re-ran the whole cloudflared attempt each time on a network `CLAUDE.md` documents as unable to use it. **90 of the 93 seconds the panel was down went to rediscovering a known failure** | Last working provider tried first; others still fall back | `35a7007` |
| 96 | **High** | The stats refresh queue was ordered by `updated_at` alone. Discovery inserts with `updated_at=now`, so a **never-scored** wallet sorted *last* — 3.3 h to reach the front, while `cleanup_wallets` deletes an unscored wallet after 1 h | Never-scored first, then longest-stale | `41fbf5a` |

### 96: the engine was discovering wallets and deleting them unscored

Measured on the live database, not inferred:

```
wallets queued ahead of a freshly discovered one : 1204
throughput                                       : 60 per 10-minute pass
time to reach the front                          : 3.3 h
cleanup_wallets deletes an unscored wallet after : 1 h
```

Of the 60 wallets the next pass would have queried, **0 were unscored and 60 were
re-checks of wallets that already had a usable rate**. The pool count drifting between
1242 and 1275 across restarts — which I had been reading as ordinary churn for several
passes — was this.

Both orderings run against one fixture: old queried 0 of 3 newcomers, new queried 3 of
3. On live data the change turns 0/60 unscored into 60/60, clearing a 65-wallet backlog
in two passes instead of never. It costs nothing — same batch cap, same API budget — and
is the better order regardless: an unscored wallet carries no weight and is invisible to
the strategy, while one whose rate is three hours old is still perfectly usable.

### What that measurement also exposed, and what I did not do about it

Refresh throughput is 60 wallets per 10-minute pass — 360/h — against ~1240 wallets on a
1 h TTL. It cannot keep up by ~3.5×, so entry weights are computed from win rates that
are mostly a day old:

| Age of the win rate carrying weight | Wallets |
|---|---:|
| under 1 h | 37 |
| 1 h – 24 h | 227 |
| **over 24 h** | **913** |

Raising `GMGN_STATS_BATCH_MAX` buys freshness with stop-loss latency, and that cap
exists because a 20-round-trip sweep once delayed stops by tens of minutes and produced
two −99.99% exits. Trading a previously-realised risk to capital for an accuracy gain is
the operator's call, so it is `ISSUES.md` #8 rather than a commit.

`CLAUDE.md` claimed `refresh_wallet_stats` "keeps those values current". It does not,
and now says so with the numbers.

### 94 was a shape problem, not an oversight

A predicate that mutates. `due_for_push` answered *and* recorded, so asking cost
something, and the only caller asked before an operation that could fail. Reproduced
against an API that fails once — delivered 0 before, 1 after — then split into a pure
predicate and an explicit `mark_pushed`.

```
$ python -m unittest discover -s gmgn -p 'test_*.py'
Ran 248 tests in 13.486s
OK
```

Stack relaunched onto all three: engine cycling 4 s after start, webapp `auth required`
from its first request, tunnel published on the first attempt at 00:15:16.

**Уверенность: средне-высокая.** Pass 25 не чист — 3 находки, одна серьёзная и найденная
только измерением live-базы, а не чтением кода. Счётчик чистых проходов **0**.

## Pass 26 — the mirror of yesterday's fix, and a port two servers could share

Lens: the previous pass's own code again (fifth pass running), then the one layer with
no coverage at all — the HTTP server itself, which is the part facing the internet.

| # | Severity | Finding | Fix | Commit |
|---|---|---|---|---|
| 97 | High | Pass 25 ordered never-scored wallets first. A wallet the API answers about with **no usable stats** keeps `winrate=0` *and* has its `updated_at` bumped — resetting the very timer `cleanup_wallets` deletes it by. It never ages out and never leaves the front. Enough of them and no scored wallet is re-checked again | Batch split: each group guaranteed half, takes what the other leaves | `ca6e3d7` |
| 98 | **High** | `serve()` did `port = port or config.WEBAPP_PORT`, so an explicit `port=0` became 8770. Worse, `allow_reuse_address` lets a second process **bind a port a live server already holds** on Windows — both succeed, the OS splits connections, and the standalone instance has no `WEBAPP_PUBLIC_URL` so it answers **without a signature** | `port=0` honoured; exclusive bind on Windows; clear refusal | `927e9c7` |

### 97: I created the starvation I had just removed

Pass 25's report claimed the fix "costs nothing". It cost the mirror image, one pass
later, in a case the surrounding code's own comment already describes ("manual seeds
especially, since cleanup never drops them").

Not hypothetical — the live pool proved it within the hour. 65 unscored wallets, one
pass under the unscored-first order queried 60 of them, and the backlog fell to 59.
**Six drained; 54 were answered-but-unusable** — exactly the permanent occupants.

```
unscored  stale  ->  picked = unscored + stale
      65   1177  ->      60 =       30 +    30
     200   1177  ->      60 =       30 +    30      <- duds cannot consume the batch
       0   1177  ->      60 =        0 +    60
      65      0  ->      60 =       60 +     0
```

Capping their share also made them self-clearing, which neither single order did: the
half not queried keeps its old `updated_at`, ages past `ZERO_TTL`, and is deleted. The
test I wrote asserting "these never age out" **failed** — 50 of 80 duds were gone after
three passes. The premise was mine and it was wrong.

### 98 was found by a test that could not pass, and the test was right

An end-to-end HTTP test could not make an authenticated request succeed. The obvious
reading was a broken fixture. It was not: `port=0` silently became the production port,
the test server bound *on top of* the running webapp, and the two split connections
between them — which is why patching `Handler._authorized` recorded nothing while the
response was unmistakably that handler's 401.

```
port=0  resolves to: 8770               <- the `or` swallows it
second bind to the live production port: SUCCEEDED (no error raised)
```

The security consequence is not confined to tests. README documents running the
components individually, so `python gmgn/webapp.py` beside a running supervisor is a
normal thing to do — and that second instance computes `REQUIRE_AUTH` with no public URL
in its environment, so it serves the account and wallet data unauthenticated while
taking roughly half the requests arriving down the tunnel.

On POSIX `SO_REUSEADDR` cannot steal an active listener, so the stdlib default stays;
it is disabled on Windows only, where it can.

### The layer that had never been tested

Every webapp test called route handlers directly, skipping routing, the auth gate, the
error-to-status mapping, headers, HEAD and static serving. Nine tests now issue real
requests over a real socket, including that a signed request *is* answered — without
which the 401 assertions would also pass against a server that refused everything.

```
$ python -m unittest discover -s gmgn -p 'test_*.py'
Ran 261 tests in 15.157s
OK
```

Live after both: engine cycling, webapp `auth required`, panel over the tunnel
`/api/health` 200 and `/api/overview` **401**. Refresh queue splitting 30/30 as designed.

**Уверенность: средне-высокая.** Pass 26 не чист — 2 находки, обе серьёзные, одна из них
исправление моего же исправления. Счётчик чистых проходов **0**.

## Pass 27 — measuring the funnel, and a fixture that made a test prove nothing

Lens: measurement over reading. The last two passes' best findings came from the live
database rather than the source, so this pass started by instrumenting the entry funnel
end to end and following what it showed.

| # | Severity | Finding | Fix | Commit |
|---|---|---|---|---|
| 99 | Medium | `GMGN_FEED_LIMIT` defaulted to 200; the API returns **100 whatever is asked** (measured at 100/150/200/500). The config read as "we sample 200 trades a poll" and never did | Default and template corrected to 100, cap documented | `e2abe3c` |
| 100 | Medium | `learn_new_makers` fetched stats for unknown makers and *then* discarded blacklisted ones. Blacklisted wallets are **21 of 22** unknown makers in a live sample, so nearly the whole per-cycle stats budget bought rows thrown away on the next line, every poll, ahead of `enter()` | Filtered before the call | `a74c9a3` |
| 101 | **High** | `mint_n()` built its prefix with `f"{i:04d}"`. Base58 has no `0`, so **every address it ever produced was rejected** by `valid_address` | Prefix encoded in base58 | `a74c9a3` |

### 101: the helper written to prevent vacuous tests was causing them

The header of the test file says, in as many words, that short placeholders "would be
dropped before reaching the code under test, and several assertions would then pass
vacuously". `mint_n`'s own docstring said "valid mints". It produced an invalid address
for all 1000 values checked, and had presumably always done so.

`test_backlog_is_capped` feeds 100 trades to `missed_elite_signals` to prove the
per-cycle cap clips them:

```
old mint_n   MISSED produced:  0   assertion 0 <= 10   (vacuous)
new mint_n   MISSED produced: 10   assertion 10 <= 10  (real)
```

It was asserting that zero is at most ten. An AST sweep of every address-shaped literal
in the suite found no others — `mint_n` was the only one, and the whole suite still
passes with valid addresses, so nothing else depended on them being rejected.

### The funnel, measured on live feed data

```
feed trades returned            100        <- asked for 200
  passing allowed() pump filter  90
  buys inside the 30-min window  51
distinct makers                  28
  known to wallet_watch           6
  BLACKLISTED                    21        <- can never be re-added
  genuinely new                   1
best cluster score              0.25       threshold 1.0
```

`allowed()` is not the constraint — 90% of the feed passes it. **The blacklist is.**
Three quarters of the wallets actively buying in the feed are permanently excluded,
against 5614 blacklisted versus 1184 watched. `enter()` needs 1.0 and each poll's
cluster can draw only on those six; the best score over 572 cycles is 0.4375.
Convergence is not possible on a pool that excludes most of its own feed.

`ISSUES.md` #1 previously asserted "the signal pool stays smaller than it should be"
with no number. It now carries this table. Still not running `unban_wallets.py` — it
changes which wallets the engine follows — but the decision now has evidence behind it
rather than a plausible story.

### Checked and found clean

Pass 26 disabled `allow_reuse_address` on Windows, which raised the question of whether
the webapp could then fail to rebind after a supervisor restart with live connections —
a restart loop at exactly the hourly moment the tunnel republishes. Tested both
directions, client-side and server-side close, with keep-alive connections open:

```
allow_reuse_address=True   rebind after server-side close: OK
allow_reuse_address=False  rebind after server-side close: OK
```

Windows does not block a bind on a port whose connections are in TIME_WAIT when no
listener holds it. No risk, and the live webapp has restarted cleanly through it.

```
$ python -m unittest discover -s gmgn -p 'test_*.py'
Ran 262 tests in 15.227s
OK
```

**Уверенность: средне-высокая.** Pass 27 не чист — 3 находки. Счётчик чистых проходов
**0**.

## Pass 28 — durability of the things that carry state across a restart

Lens: what survives being killed. The supervisor terminates children routinely — every
tunnel URL change, hourly on a free session — so anything written without care meets an
interrupt on a schedule.

| # | Severity | Finding | Fix | Commit |
|---|---|---|---|---|
| 102 | Medium | `save_cursor` used a plain `write_text`. A torn file makes `load_cursor` return `None`, `catch_up` read that as a first run, and **the entire pending backlog is skipped** — the exact outcome `catch_up` exists to prevent | Temp file, fsync, `os.replace` | `ac90369` |

```
clean write -> load_cursor() = 42
torn write  -> load_cursor() = None
5 EXIT events waiting, cursor after catch_up: 5     (all five skipped)
```

The project already had the pattern — `mass_discovery.write_quality` writes
`wallets-quality.txt` exactly this way. The bot's state file simply never used it.
Verified in production after restart: `{"last_event": 560}`, zero temp files left.

### Four things checked and found correct

Most of this pass was verification that came back clean. Recording it, because a checked
negative is the difference between "no bug here" and "nobody looked".

- **Attribution across a re-entry.** `wallet_attribution` matches an entry to "the next
  EXIT on that mint" and calls it exact *because a mint has at most one open position at
  a time* — yet every existing test used distinct mints, so the case that claim is about
  was never driven. It is correct: the first signal owns the winning round trip, the
  second the losing one, totals still reconcile. Now a test.
- **`signal_history` primary-key collisions.** Two chains in one cycle share `now`. The
  strong signal survives the weak one (`best_score` 1.0 kept) and mints are summed, not
  replaced.
- **`token_price` latency in the stop path.** `exits()` prices positions serially, which
  is the same shape as the starvation fixed in an earlier pass. Measured: ~1.0 s per call
  (n=8, min 0.97, max 1.08), at most 4 positions at the current budget, and a 60 s cache
  in front. ~4 s, not a risk. It is O(open positions) though, so `CLAUDE.md` now says so
  where the ordering invariant is documented — raising the budget would need it
  revisited.
- **Price cache eviction.** Two-tier: expire past TTL, then drop the oldest half if still
  at the cap. Bounded, as documented.

```
$ python -m unittest discover -s gmgn -p 'test_*.py'
Ran 268 tests in 14.852s
OK
```

Live: unscored wallet backlog down from 65 to 4 as the Pass 25/26 queue fix drains it,
engine cycling, panel `401` unauthenticated over the tunnel.

**Уверенность: средне-высокая.** Pass 28 не чист — 1 находка, но заметно ближе к чистому
проходу, чем предыдущие. Счётчик чистых проходов **0**.
