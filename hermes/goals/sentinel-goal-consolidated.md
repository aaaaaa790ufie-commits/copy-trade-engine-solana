# GOAL: Sentinel — fix remaining bugs, then run unattended paper-trading and report on return

Continuation of `~/sentinel` (06d6de3). Original `/goal` doc's rules still
apply in full (Fasol isolation, unfunded wallet, double-gate DRY_RUN+LIVE,
dependency allowlist, no secrets in prompts, read-then-report before
claiming anything done, PROGRESS.md as ground truth). This doc has two
parts: fix the two known bugs first (Sections 1-3), then start an
unattended accumulation run and prepare a report for when the human
returns (Sections 4-5). Do them in order — don't start accumulating on
top of an unfixed pricing bug.

## 0. Read-then-report

Read `wait_for_slot()` and the spot-check script as they currently exist
before changing either — confirm exactly how `wait_for_slot()` waits
(polling loop? what interval?) and exactly why the spot-check script used
`getProgramAccounts` for Pump.fun instead of the same PDA derivation the
live pipeline uses.

## 1. Replace polling with a non-polling wait

`getSlot()` in a loop is the exact anti-pattern this whole project was
built to avoid (see original doc Section 2 — this is what rate-limited
Fasol). Replace it with one of:

- Reuse slot notifications already arriving over the existing WS
  subscription in `ingest/` (if slots are observable there, e.g. via
  transaction notifications carrying slot numbers) instead of a separate
  RPC channel, or
- If no slot stream is easily available, `sleep()` for the expected lag
  duration (2 slots × ~400ms + a small margin) and then do a single
  `getAccountInfo` read — one RPC call instead of a polling loop.

Confirm the fix doesn't add per-trade RPC calls beyond what's already
budgeted (one `getAccountInfo` for pool state).

## 2. Fix the spot-check script to use the real resolution path

The spot-check must call the exact same pool-resolution method the live
pipeline uses (PDA derivation for Pump.fun/PumpSwap, Raydium API v3 for
AMM v4/CPMM) — not a different, more expensive method that happens to be
broken on free tier.

## 3. Actually run the spot-check and record real numbers

Pick 3-5 already-logged trades, compute what their lagged price would be,
and put the actual before/after numbers in `PROGRESS.md` — signal price,
lagged price, % difference, and a one-line sanity read (does the
direction/magnitude make sense, or does something look off — sign flip,
order-of-magnitude error). Flag any implausible case plainly rather than
averaging it away.

If Section 3 turns up a real problem with the pricing formula, fix that
before moving to Section 4 — don't accumulate hours of data on a broken
formula just to hit a time target.

## 4. Start unattended continuous paper-trading

Once Sections 1-3 are clean: start the full pipeline (ingest → scorer →
filter → risk → executor DRY_RUN with lagged pricing → position_mgr)
running continuously and left running while the human is away. No time
limit is set — let it run and accumulate for as long as the session
allows. Keep `PROGRESS.md` updated periodically (not just once at the
end) with a running trade count, so a check partway through still shows
real state if the session gets cut short.

Do not change scoring thresholds, risk parameters, or the venue list to
manufacture more trades faster — the point is honest data at whatever
rate the real strategy produces it.

## 5. Prepare a return-ready report

Before wrapping up (or periodically, in case the session ends
unexpectedly), write a `SESSION_REPORT.md` at the repo root — this is
the first thing the human reads on return, so it must be self-contained
and numbers-only where possible, not a narrative recap:

- Wall-clock duration the pipeline actually ran.
- Total trades logged, broken down by venue and by `pricing_method`
  (`naive` vs `lagged`) — if most rows are still `naive`, say why.
- Tracked wallets by tier (A/B/C counts), and how many tier-A wallets
  actually produced a copy signal during the run (a scored-but-silent
  wallet is a different situation from one that never triggered).
- Raw PnL/win-rate vs. lag+fee+slippage-adjusted PnL/win-rate, side by
  side — this comparison is the actual answer to "does this look worth
  funding," more than either number alone.
- Any errors, RPC rate-limit hits, or blocked venues encountered during
  the run, with counts, not just "some errors occurred."
- Explicit statement of what's still `UNVERIFIED` or unresolved, so nothing
  gets silently upgraded in status just because the run completed.

Keep updating this file rather than replacing it with a final version at
the very end, in case the free-tier model session ends before a "final"
version would've been written — a `SESSION_REPORT.md` that's slightly
stale beats one that doesn't exist because the session cut out first.

No live execution, no wallet funding, no touching Fasol — unchanged from
the original doc, `/yolo`-compatible, no permission needed for any of
Sections 0-5.
