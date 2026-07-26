# Open issues

Findings that are real but were **not** fixed autonomously, because acting on them
changes trading behaviour, mutates existing data, or depends on a judgement call
that is the operator's to make. Each entry states what is wrong, what it costs,
and what the decision is.

Fixed issues are not listed here — they are in `PROGRESS.md` and the git history.

---

## 1. 5551 wallets are blacklisted, an unknown share of them wrongly

**Status:** remediation written, not run. Needs approval.

Until `a2d7e16`, `refresh_wallet_stats` rewrote a wallet's win rate to a synthetic
`0.49` when it had too small a sample or no buys in the sampled window, so that
`cleanup_wallets` would sweep it up. The sweep blacklists permanently, and
discovery never re-adds a blacklisted address. A dormant wallet with an 80% win
rate was therefore banned forever.

The logic is fixed, but the damage is already in `sentinel.db`: 5551 blacklist
rows against 1205 active watched wallets. The two causes are indistinguishable
after the fact — both are stored as `reason='low_winrate'` with no record of the
win rate that triggered them.

**Cost of leaving it:** the signal pool stays smaller than it should be, which
directly reduces how often a genuine multi-wallet cluster can form.

**Remediation:** `python gmgn/unban_wallets.py --apply` clears `low_winrate`
entries so the engine re-evaluates them under the corrected logic. It takes a
backup via SQLite's online backup API first, and defaults to a dry run. Wallets
that really do trade below 50% are re-banned on their next stats refresh; the
engine only ever weights a wallet on a freshly fetched win rate, so nothing
unverified can enter the pool in the meantime.

**Not done automatically because** it changes which wallets the engine will follow.

---

## 2. The strategy loses money, and that is not a bug in the code

**Status:** reported, no action taken. Needs a decision.

As of 2026-07-26 the paper account is at ~0.006 SOL of an initial 0.1, across 10
closed trades with 4 winners. The engine now does what it was specified to do —
the earlier −99.99% exits were a real defect and are fixed — but doing it
correctly is still unprofitable at the current settings.

The dominant parameter is `GMGN_ENTRY_SCORE=0.25`, which equals the weight of a
single wallet at 70%+. "Weighted convergence" therefore reduces to "follow one
wallet", with no convergence required at all. Raising it to 0.3125 would require
one 70% wallet plus one 50%+ wallet; 0.5 would require two 70% wallets.

**Not done automatically because** entry threshold, stake and stop distances are
risk parameters, and were explicitly placed off-limits without approval.

A backtest over `paper_trades` would quantify this rather than guess, but the
sample (10 closed trades) is far too small to conclude anything yet.

---

## 3. `engine_events` and `paper_trades` grow without bound

**Status:** open, low urgency.

Neither table has a retention policy. At the current rate (`engine_events` reached
~500 rows in a day, dominated by `WALLET` messages) this is years away from
mattering on disk, but it does make the bot's startup scan and the webapp's
`MAX(event_ts)` lookups slowly more expensive. Indexes on `event_ts` are in place,
so the practical impact today is nil.

**Not done automatically because** deleting history is irreversible and the right
retention window is a preference, not a fact.

---

## 4. A delisted token locks its stake open forever

**Status:** detected and alerted, not resolved. Needs a decision.

`exits()` can only close a position it can price. If a token stops being quotable —
the normal end state for a rugged memecoin — the position stays `open` past its max
hold indefinitely, and its stake stays deducted from the balance. The account can
never recover that capital, and `/status` keeps reporting it as an open position at
a stale mark.

Since `28cd2a2` the engine raises a `STUCK` event once the position is
`GMGN_STUCK_AFTER_SECONDS` old (default 2× max hold) and repeats at most every
`GMGN_STUCK_REMIND_SECONDS`, so it is visible rather than silent.

**What is not decided:** what such a position is worth. The honest answer for a
delisted memecoin is usually zero, but writing off the full stake automatically is
a real loss booked on an assumption. The alternatives — closing at the last known
mark, or at the hard-stop level — are each defensible and each wrong in some cases.

**Not done automatically because** it books a realised loss on the operator's
account based on an inference, not an observed price.

---

## 5. `robinhood` chain support is unverified

**Status:** open, documented.

`allowed()` returns `True` unconditionally for `chain == "robinhood"`, bypassing
the pump.fun launchpad filter that every Solana token must pass. The default chain
list is now `sol` only, so this path is dormant, but it is untested and would
admit every token on that chain if enabled.

**Not done automatically because** removing it would drop a feature someone may
intend to use; verifying it needs a working robinhood API key and live data.
