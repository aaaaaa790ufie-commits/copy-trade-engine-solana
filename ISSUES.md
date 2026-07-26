# Open issues

Findings that are real but were **not** fixed autonomously, because acting on them
changes trading behaviour, mutates existing data, or depends on a judgement call
that is the operator's to make. Each entry states what is wrong, what it costs,
and what the decision is.

Fixed issues are not listed here — they are in `PROGRESS.md` and the git history.

---

## 1. 5576 wallets are blacklisted, an unknown share of them wrongly

**Status:** remediation written, not run. Needs approval.

Until `a2d7e16`, `refresh_wallet_stats` rewrote a wallet's win rate to a synthetic
`0.49` when it had too small a sample or no buys in the sampled window, so that
`cleanup_wallets` would sweep it up. The sweep blacklists permanently, and
discovery never re-adds a blacklisted address. A dormant wallet with an 80% win
rate was therefore banned forever.

The logic is fixed, but the damage is already in `sentinel.db`: 5576 blacklist
rows against 1188 active watched wallets (counts as of 2026-07-26 21:00). The two causes are indistinguishable
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

## 2. Whether eleven trades justify any conclusion at all

**Status:** two of the three decisions have been taken. The third stands.

Two changes were made on the operator's instruction on 2026-07-26:

* `GMGN_ENTRY_SCORE` raised from 0.25 to **1.0**, and the weight ladder given a 1.0
  tier at 90% and 0.5 at 80–90%. Entry now needs one wallet at 90%+, or two at 80–90%,
  or four at 70–80%. Under the old pairing a single 70% wallet was a full signal and
  every entry fired on `wallets=1`, so "weighted convergence" never converged. 155 of
  1188 watched wallets can now enter alone, against 318 before.
* The account was **settled and restored to 0.1 SOL** (`gmgn/reset_account.py`). The
  top-up raised `initial_budget_sol` by the same amount, so lifetime P&L still reads
  −0.06899 rather than pretending the loss did not happen, and `reset_at` lets both the
  panel and `/status` report the new settings' results separately.

**What remains undecided, and is the substantive point:** eleven closed trades cannot
distinguish a bad strategy from an unlucky one. The changes above are defensible on
reasoning — a threshold equal to a single wallet's weight is not convergence by any
reading — but they are not yet *evidenced*, and neither was the configuration they
replaced. The remedy is more trades, not more tuning. Resist re-tuning on the next ten.

### Update 2026-07-26: the 1 h cap is supported by the historical data

An earlier note in `PROGRESS.md` suggested the trailing stop might be near-inert at a
1 h hold, on the grounds that only one of twelve trades exited above +25%. **That
reasoning was wrong** — it measured P&L at exit rather than the peak that arms the
trailing stop. Measured properly:

| Exit reason | Count | Holding times |
|---|---:|---|
| trailing stop 15% | 3 | 9, 25, 34 min |
| hard stop −45% | 4 | 12, 436, 671, 671 min |
| max hold | 4 | 379, 428, 524, 834 min |
| strategy reset | 1 | 107 min |

The trailing stop is not inert: it produced **every profitable exit** (+18.39%,
+18.74%, +31.48%), and all three closed within 34 minutes, comfortably inside the new
cap. Meanwhile the eight positions that lived past an hour lost **−0.0747 SOL between
them**, including both −99.99% write-offs.

On this sample the 1 h auto-close cuts precisely the cohort that lost money and leaves
the winners untouched. Eleven trades is still far too small to call it settled, but the
direction of the evidence now points the same way as the change.

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

## 5. `paper_positions` is keyed by mint alone, not by (mint, chain)

**Status:** open. Dormant while `GMGN_CHAINS` is `sol` only.

`paper_positions(token_mint TEXT PRIMARY KEY)` has no chain in its key, while
`paper_cooldowns`, `wallet_watch` and `token_scores` all key on `(…, chain)`. If two
chains were ever polled and the same mint string appeared on both, the second entry
would overwrite the first through `enter()`'s `ON CONFLICT DO UPDATE`, silently
replacing one open position with another — including its `chain`, `entry_price` and
`stake_sol`. The account would then hold one position where it had paid for two.

Solana mint addresses are effectively unique, so this cannot bite on the current
configuration, and the same is true of any single-chain setup.

**Not done automatically because** changing a primary key means rebuilding the table on
the operator's live database. That is a migration, not an edit, and it is not worth
running against real data to fix a path that is switched off. If a second chain is ever
enabled, this must be fixed first.

---

## 6. `robinhood` chain support is unverified

**Status:** open, documented.

`allowed()` returns `True` unconditionally for `chain == "robinhood"`, bypassing
the pump.fun launchpad filter that every Solana token must pass. The default chain
list is now `sol` only, so this path is dormant, but it is untested and would
admit every token on that chain if enabled.

**Not done automatically because** removing it would drop a feature someone may
intend to use; verifying it needs a working robinhood API key and live data.

---

## 7. Most of the repository is not part of the documented project

**Status:** open. Needs a scope decision from the operator — I will not delete code
on an inference.

`CLAUDE.md` documents `gmgn/` and nothing else. The repository also contains a Rust
workspace (`Cargo.toml`, `src/`, `ingest/`, `filter/`, `scorer/`, `risk/`, `executor/`,
`position_mgr/`, `telemetry/`) and several Python components that predate it. None of it
is started by `supervisor.py`, referenced by the engine, or covered by the test suite.

What was established rather than assumed:

| Component | Evidence about whether it is live |
|---|---|
| `gmgn/monitor.py` | Writes `gmgn_signals` for "the Rust engine". That table **does not exist** in `sentinel.db` — it has never run against this database |
| `dashboard/app.py` | Needs `streamlit`, which is **not installed**; the project is otherwise stdlib-only. Reads `candidate_wallets`, `discovered_tokens`, `wallet_trades`. Its "System Status" panel is a hardcoded string, not real state |
| `discovery/`, `scorer/` | Populate the legacy tables. `wallet_trades` and `wallet_scores_v2` are **empty**; `candidate_wallets` 13 rows, `discovered_tokens` 21, `wallet_scores` 17 — all stale |
| Rust workspace | Not built or invoked anywhere in this project's workflow |

The one live coupling: `run_engine.import_old_wallets` reads `wallet_scores` and
`candidate_wallets` into `wallet_watch` on every start. It logged `imported 0 wallets`
on the last boot, because everything importable is already there. Addresses are
validated by `_admit`, so the legacy tables cannot inject anything malformed.

**Cost of leaving it:** no runtime cost. The cost is to review — the dead code is
indistinguishable from live code at a glance, and it is why `monitor.py` went twenty-one
passes without being read. Two real defects were found in it on first reading (see
Pass 22), in a file nothing runs.

**What I did anyway:** fixed those defects rather than leaving known-broken code in
place, on the grounds that "probably dead" is not "dead".

**The decision:** whether to delete the legacy tree, move it to an `archive/` branch, or
keep and document it. Deleting is right if the Rust engine is abandoned; it is wrong if
it is a parallel effort. That is not something the code can tell me.
