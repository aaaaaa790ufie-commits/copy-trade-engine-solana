# GOAL: Sentinel — resolve pool addresses and complete lagged fill pricing

Continuation of `~/sentinel` (848f5f8, schema V2 live). Original `/goal`
doc's rules still apply in full. This is the specific blocker from last
session: "pool-state fetch + price calc deferred to when pool addresses
are resolved" — this doc is that resolution step.

## 0. Read-then-report

Read the current executor code where `pricing_method` is set and
`source_slot` is consumed, and confirm exactly what's missing to go from
`naive` to `lagged` for each venue. Don't assume it's identical across
venues — Pump.fun's bonding curve and Raydium's pool accounts are
resolved differently.

## 1. Pool-address resolution per venue (do this the cheap way, not the RPC-heavy way)

Free-tier RPC budget matters here — avoid `getProgramAccounts` scans if a
deterministic or API-based alternative exists:

- **Pump.fun**: the bonding-curve account is a PDA derived from the token
  mint (seeds include `"bonding-curve"` + mint pubkey) — compute this
  locally, no RPC call needed to *find* it, only to read its state after.
- **PumpSwap**: check if pool address is similarly PDA-derivable from the
  mint pair; if not, check whether PumpSwap exposes a lookup API before
  falling back to RPC scanning.
- **Raydium AMM v4 / CPMM**: use Raydium's own public pool-list API
  (`https://api-v3.raydium.io/main/info` per their docs — confirm the
  exact endpoint and pool-lookup-by-mint path in their current API docs,
  it may have a more specific endpoint than the general info one) instead
  of `getProgramAccounts` with memcmp filters — the latter is expensive
  and slow on a free-tier RPC connection and Raydium's API exists
  specifically so integrators don't have to do that.
- Cache resolved pool addresses (mint → pool address) in SQLite once
  found — this is a lookup you don't want to repeat per-trade.

## 2. Implement the actual lagged price calculation

Once pool addresses resolve:

- On a copy signal, after the configured lag (slots from `config.toml`),
  read the pool's current state via `getAccountInfo` (this is the point
  where an RPC call is actually needed and justified).
- Compute fill price via the venue's real formula: Pump.fun bonding curve
  formula (constant product against virtual reserves), Raydium AMM
  v4/CPMM constant-product against real reserves.
- Set `pricing_method = 'lagged'` and populate `simulated_fill_price_sol`
  from this calculation, alongside the existing raw fields from schema V2.
- If a pool can't be resolved for a given signal (new/unindexed token,
  API miss), fall back to `pricing_method = 'naive'` for that row rather
  than blocking the trade log — log the fallback reason.

## 3. Verify against a handful of real trades before trusting it broadly

Before treating `lagged` rows as reliable: pick 3-5 already-logged `naive`
trades, manually recompute what their `lagged` price would have been, and
sanity-check the numbers look plausible (fill price should differ from
signal price by a small, explainable amount — not wildly off, not
identical). Note this spot-check in `PROGRESS.md` with the actual
before/after numbers, not just "looks fine."

## 4. What "done" means

Report back with: pool-resolution method actually used per venue (quote
the code, not a description), confirmation `lagged` pricing_method rows
are appearing in the live trade log with real numbers, and the Section 3
spot-check results. Then resume continuous accumulation — this is the
last known blocker on trusting the paper-trading numbers, so once this
lands, accumulated data going forward should be treated as usable for the
"paper vs live" conversation, not before.
