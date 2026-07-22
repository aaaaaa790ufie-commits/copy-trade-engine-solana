# GOAL: Sentinel — finish Executor coverage, wire realistic paper-fills, start continuous paper-trading

Continuation of `~/sentinel` (currently 6 commits, abf18a5 → c382ead, Helius
WS connected). Original `/goal` doc's rules still apply in full (Fasol
isolation, unfunded wallet, double-gate DRY_RUN+LIVE, dependency allowlist,
no secrets in prompts, read-then-report before claiming anything done,
PROGRESS.md as ground truth). This doc adds three concrete objectives.

## 0. Read-then-report reminder

Before touching code: open and actually read the current state of
`src/executor/` (or wherever venue logic lives), `PROGRESS.md`, and
`config.toml`. Confirm which 2 of 4 venues are actually implemented and
which 2 are missing — don't assume from the last chat summary, verify by
reading. Report the actual current venue list back before starting work.

## 1. Close the remaining Executor venue gap

Implement instruction construction for the venues not yet covered (per
the original doc's Section 4/6: Pump.fun, PumpSwap, Raydium AMM v4/CPMM —
confirm against what's actually missing per Section 0's read-first check,
don't guess which 2 are done). For each newly-added venue:

- Follow the same pattern already used for the 2 working venues — read
  those first as the reference implementation, don't invent a new style.
- Every venue's instruction encoding must be **cross-checked against a
  real historical on-chain transaction** for that venue (pull one via free
  RPC `getTransaction`, compare your constructed instruction's accounts/
  data layout against it byte-for-byte where feasible) before it stops
  being marked `UNVERIFIED` in `PROGRESS.md`. This was the exact gap
  flagged last time — don't repeat it. A venue with no cross-check stays
  `UNVERIFIED`, full stop, even if the code compiles and looks plausible.
- If a venue turns out to be substantially harder than the other three
  (unusual account structure, undocumented instruction layout), log the
  specific blocker in `PROGRESS.md` and move on rather than stalling — 3/4
  verified venues with one honestly logged as blocked beats 4/4 claimed
  with one silently unverified.

## 2. Wire the realistic paper-fill model (original doc Section 6.1) if not already done

Read-first: check whether execution lag, price-impact-via-pool-state, DEX
fee, and network-cost-assumption are actually implemented in the fill
simulation path, or whether DRY_RUN currently just logs "would buy at
signal price" (naive, and known to be misleadingly optimistic). If it's
the naive version, implement Section 6.1 properly:

- Fill price = pool state N slots after the source wallet's fill (not the
  source wallet's own price), computed via local bonding-curve/constant-
  product math against `getAccountInfo`.
- Subtract venue swap fee (bps) and an assumed priority-fee+tip cost
  (config value) from simulated PnL on both entry and exit.
- Persist both the naive/raw number and the lag+fee+slippage-adjusted
  number per trade in SQLite — telemetry needs to show both so the human
  can see how much "edge" survives contact with reality.

## 3. Start continuous paper-trading and let it accumulate

Once Sections 1-2 are done for at least the originally-scoped venues:

- Start the full pipeline (ingest → scorer → filter → risk → executor
  DRY_RUN → position_mgr) running continuously, not as a one-shot test.
- This session doesn't need to sit and wait for days of data — set it up
  to run persistently (background process, or documented restart
  instructions if the runtime doesn't support long-running background
  execution) and keep updating `PROGRESS.md` with a running count: trades
  logged, venues represented, tracked wallets by tier, wall-clock time
  since start. A few hours of partial data by end of this session is a
  fine outcome; a correctly-running unattended pipeline matters more than
  hitting a specific trade count in one sitting.
- Do not change any scoring thresholds, risk parameters, or venue list
  specifically to produce more trades faster — the point is honest data
  at whatever rate the real strategy produces it, not inflated activity.

## 4. What "done" means for this goal

Report back only once: read-then-report confirms venue count 1 (Section
0), all previously-missing venues are either verified-and-working or
honestly logged as blocked (Section 1), the fill model change is verified
by reading the actual diff/file (Section 2), and the pipeline is
confirmed running (Section 3) — with real numbers from `PROGRESS.md` and
the SQLite trade log quoted in the summary, not a recalled description of
what was built. No live execution, no wallet funding, no touching Fasol —
unchanged from the original doc.
