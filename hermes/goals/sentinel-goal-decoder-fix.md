# GOAL: Sentinel — implement decode_swap_event() for real, with proof, not a claim

Start this as a **fresh session (`/new`)**, not a continuation of the
compressed one — the last session hit 3x context compression and, in that
state, marked the pipeline "done" while its core decoder was a stub
returning `None` unconditionally. That gap needs full attention, not
whatever context survived compression. Original `/goal` doc's rules still
apply in full (Fasol isolation, unfunded wallet, double-gate, no live
execution).

## 0. Read-then-report

Read `decode_swap_event()` as it currently exists. Confirm it's really a
stub (per the last session's own admission) and read what it's supposed to
receive as input (raw transaction/log data from the WS subscription) and
what a `SwapEvent` is supposed to contain (per `ingest/` module's original
design — venue, wallet, mint, direction, amount, slot).

## 1. Implement real decoding, per venue

For each of the 4 venues, parse the actual transaction logs/instruction
data arriving over the WS subscription into a populated `SwapEvent`:

- Pump.fun: parse the buy/sell instruction data using the discriminator
  convention already established in the executor code (reuse it, don't
  reinvent).
- PumpSwap, Raydium AMM v4, Raydium CPMM: parse using the IDL/instruction
  layouts already verified for the executor path — the decode side and
  the execute side should reference the same instruction-layout
  definitions, not duplicate/diverge them.
- If a transaction's venue can't be identified or data doesn't match a
  known layout, return `None` for that one and increment a counter — but
  the function must return `Some(SwapEvent)` for the transactions it can
  actually parse, which should be the large majority of what's arriving
  from a program-level log subscription.

## 2. Hard verification bar — do not mark this done without doing this

"I implemented the parser" is not sufficient — the last several sessions
established a repeated pattern of marking things done based on the code
existing/compiling rather than the code actually producing correct output
on real data. For this specific function, the bar is:

- Run the pipeline against live WS traffic for at least 2-3 minutes.
- Capture and print/log at least 5 actual non-`None` decoded `SwapEvent`s
  with their real field values (venue, wallet pubkey, mint, direction,
  amount, slot).
- Put those 5 real examples directly in `PROGRESS.md` — not "it works,"
  the actual decoded values. If you cannot produce 5 real examples within
  a few minutes of live traffic, the function is not done, regardless of
  whether it compiles or looks correct on inspection.
- Additionally report: out of all program-log notifications received in
  that window, what fraction decoded successfully vs. returned `None` —
  this tells the human how much of the venue coverage claimed in earlier
  sessions is actually reachable from real traffic.

## 3. Only after Section 2's bar is met: resume accumulation

Restart the unattended pipeline (Section 4 of the previous goal) now that
the decoder actually works, and let `SESSION_REPORT.md` update with real
trade counts this time. Keep the same discipline as before: update the
report periodically, don't tune parameters to inflate trade counts, note
anything still `UNVERIFIED`.

## 4. What "done" means

Report back with the 5 real decoded examples from Section 2 quoted
directly, the success-rate fraction, and confirmation the pipeline is
running again with a non-zero trade count building in `SESSION_REPORT.md`.
No live execution, no wallet funding, no touching Fasol.
