# GOAL: Sentinel — verify remaining venues, add lag-based fill price, future-proof the trade log

Continuation of `~/sentinel` (11 commits, f787ab7). Original `/goal` doc's
rules still apply in full (Fasol isolation, unfunded wallet, double-gate
DRY_RUN+LIVE, dependency allowlist, no secrets in prompts, read-then-report
before claiming anything done, PROGRESS.md as ground truth). This doc adds
three concrete objectives, in order — do #2 before letting more trades
accumulate, since it affects what gets logged.

## 0. Read-then-report reminder

Read the actual current `PROGRESS.md`, the Pump.fun and Raydium AMM v4
venue code, and the SQLite schema for `wallet_trades` before touching
anything. Confirm what you find, don't assume from the last summary.

## 1. Close the verification gap on Pump.fun and Raydium AMM v4

PumpSwap and Raydium CPMM were marked "IDL-VERIFIED" with a named source
(pump_amm.json, raydium_cp_swap.json). Pump.fun and Raydium AMM v4 were
marked ✅ without a comparable verification artifact — just a description
of how the instruction is encoded, not what it was checked against. Bring
these two up to the same standard:

- Find or fetch the official IDL for Pump.fun and Raydium AMM v4 if one
  exists publicly, same as was done for the other two venues. If no IDL
  is available for one of them, fall back to the original method: pull a
  real historical transaction for that venue via free RPC `getTransaction`
  and compare your constructed instruction's accounts and data layout
  against it directly.
- Only remove `UNVERIFIED` for a venue once you can name, in `PROGRESS.md`,
  the specific artifact or transaction signature it was checked against —
  the same way PumpSwap and Raydium CPMM already document theirs. "Ports
  the same pattern as a verified venue" is not itself verification.
- If either venue genuinely can't be verified this way (no IDL, and you
  can't isolate a clean reference transaction), leave it `UNVERIFIED` and
  say so plainly — don't upgrade the status without the artifact to back it.

## 2. Implement N-slots-later fill pricing (the actual point of Section 6.1)

This was logged as "future work" last time, but it's the part that
determines whether the PnL/win-rate numbers mean anything — fee deduction
alone still lets entry/exit price be optimistic. Implement it now, before
more trades accumulate under the naive pricing:

- On a copy signal, record the slot/timestamp of the source wallet's fill.
- Wait the configured lag (default 2 slots — reuse whatever's already in
  `config.toml` from the original doc if it's there, add it if not) before
  computing the simulated fill.
- Compute the simulated fill price from the pool's state *at that later
  slot*, not at the signal slot — via the same local bonding-curve/
  constant-product math already used for price impact, just evaluated
  against the pool state N slots on. This is a live on-chain read
  (`getAccountInfo`), not a stored/replayed value.
- This changes `simulated_fill_price_sol` going forward. Existing rows
  computed under the naive (signal-price) method should be flagged
  somehow (e.g. a `pricing_method` column: `naive` vs `lagged`) rather
  than silently mixed with new rows — a human reading the trade log later
  needs to know which rows are comparable to which.

## 3. Future-proof the trade log: persist raw inputs, not just derived numbers

Check whether `wallet_trades` currently stores the raw signal data
(`signal_slot`, `pool_address`, `signal_timestamp`, position size, venue)
alongside the derived `simulated_fill_price_sol` / `network_fee_sol`. If it
only stores derived numbers, add the raw fields now, going forward — the
motivation: if the pricing/fee model needs to change again later (it
already did once), historical rows should be recomputable from raw inputs
instead of needing to be re-collected from scratch. This doesn't require
backfilling old rows retroactively if the raw data for them wasn't kept —
just stop the bleeding going forward, and note in `PROGRESS.md` from what
row/timestamp onward raw fields are actually available.

## 4. What "done" means for this goal

Report back once: Section 0's read-first is done, Section 1's two venues
either have a named verification artifact or are honestly still
`UNVERIFIED`, Section 2's lag-based pricing is live and old vs. new rows
are distinguishable, and Section 3's raw fields are confirmed present in
new rows (quote the actual schema, not a description of intent). Then
resume continuous paper-trading accumulation as before — no live
execution, no wallet funding, no touching Fasol.
