# GOAL: Sentinel — switch WS subscription from program-wide to per-wallet mentions

Continuation of `~/sentinel`. Original `/goal` doc's rules still apply in
full. This is the actual fix for the Helius credit burn confirmed last
session: `logsSubscribe` currently uses `mentions: [program_id]` (4
programs), which delivers every matching transaction on all of Solana
mainnet, not just the ones touching tracked wallets — that's the 86.9%
WebSocket cost and the 100% wasted `getTransaction` calls.

## 0. Read-then-report

Read `src/ingest.rs` around `subscribe_program_logs` (lines ~575-603) and
confirm how `TRACKED_PROGRAMS` and the tracked-wallet list currently
relate to each other in the subscription setup.

## 1. Switch to per-wallet mentions subscriptions

- Replace the 4 program-wide `logsSubscribe` calls with one
  `logsSubscribe` per tracked wallet, using
  `"mentions": [wallet_pubkey]` instead of `"mentions": [program_id]`.
- This subsumes the venue-wide subscriptions — a wallet's swap on any of
  the 4 venues will still surface, since `mentions` matches any account
  key in the transaction, not a specific program. Venue is determined
  during decode as before, just on a much smaller inbound stream.
- Check whether Helius's free tier caps the number of concurrent
  `logsSubscribe` filters per WS connection. If there's a cap and it's
  below the current tracked-wallet count (or would be once discovery
  finds more), note the number in `PROGRESS.md` and, if needed, split
  wallets across multiple WS connections rather than silently dropping
  coverage for wallets beyond the cap.
- Keep the existing decode pipeline (WS-log-based decode first, fallback
  to `getTransaction` only when needed) exactly as already implemented —
  this is on top of that, not instead of it.

## 2. Verify the fix actually reduces usage

- Restart the pipeline, let it run for 15-30 minutes.
- Compare Helius dashboard credit usage rate (credits/hour) before vs.
  after this change — pull the actual before number from the prior
  session's data (574,533 used over ~4 days per the dashboard screenshot)
  and report the after rate from the same dashboard.
- Report in `PROGRESS.md`: WS message volume before/after (rough order of
  magnitude is fine — "went from N events/min network-wide to M
  events/min matching tracked wallets"), and confirm `getTransaction`
  calls are now only firing for events that already matched a tracked
  wallet.

## 3. What "done" means

Report back with the actual subscription-count limit finding (or
confirmation there isn't one on this tier), the before/after credit rate
comparison with real dashboard numbers, and confirmation trades are still
being correctly detected for the 4 tracked wallets (or, if none trade
during the verification window, confirmation the subscription itself is
alive — e.g. a heartbeat/connection-count log). No live execution, no
wallet funding, no touching Fasol.
