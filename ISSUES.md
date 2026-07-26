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

**Cost of leaving it, now measured rather than asserted.** Taken against a live feed
sample on 2026-07-27, counting distinct wallets that actually placed a buy:

| Buying makers in one feed poll | 28 |
|---|---:|
| known to `wallet_watch` | 6 |
| **blacklisted — can never be re-added** | **21** |
| genuinely new | 1 |

**Three quarters of the wallets actively trading in the feed are locked out**, against a
blacklist of 5614 versus a watch list of 1184. This is the mechanism behind the entry
drought: `enter()` needs a weighted score of 1.0, the cluster it builds each poll can
only draw on those 6, and the best score seen over 572 cycles is 0.4375. It cannot
converge on a pool that excludes most of its own feed.

That is not on its own proof the bans were wrong — a genuinely bad trader appearing in
the feed is exactly what the blacklist is for. But the sweep that produced most of these
entries could not distinguish "confirmed sub-50%" from "not enough data yet", and 21 of
22 is far above the share of feed participants one would expect to be genuinely bad.

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
| `SESSION_REPORT.md`, `config.toml` | Describe that pipeline's run parameters. `config.toml` is read by nothing in `gmgn/`; the report is now labelled historical rather than left to read as current |

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

---

## 8. Entry weights are computed from win rates that are mostly a day old

**Status:** open. The prioritisation bug behind it is fixed; the capacity limit is a
risk trade-off and is the operator's to make.

`cached_winrates()` supplies the weights every entry decision is made from.
`refresh_wallet_stats` renews them, capped at `GMGN_STATS_BATCH_MAX × 10` wallets per
maintenance pass — 60 per 10 minutes, 360/h — against ~1240 wallets on a 1 h
`GMGN_STATS_TTL_SECONDS`. It cannot keep up by roughly 3.5×, so the pool is permanently
behind. Measured 2026-07-27 on the live database:

| Age of the win rate carrying weight | Wallets |
|---|---:|
| under 1 h | 37 |
| 1 h – 24 h | 227 |
| **over 24 h** | **913** |

**Cost of leaving it:** a wallet is weighted on what it was doing yesterday. That cuts
both ways — a wallet that has since collapsed still carries a 90% weight and can open a
position on its own, and one that has improved is under-weighted. It does not corrupt
the books or risk capital beyond a single stake, but it does mean "90% win rate" in the
panel is a claim about the recent past, not the present.

**What was already fixed** (Pass 25): the refresh queue was ordered by `updated_at`
alone, so a newly discovered wallet — inserted with `updated_at=now` — sorted *last*,
behind 1204 already-scored ones. It needed 3.3 h to reach the front while
`cleanup_wallets` deletes an unscored wallet after 1 h, so the engine was discovering
wallets and deleting them without ever scoring them; the pool count visibly oscillated.
Never-scored wallets now come first. That costs nothing — same batch cap, same API
budget — but it does not change throughput.

**The decision:** raising `GMGN_STATS_BATCH_MAX` (or shortening
`GMGN_MAINTENANCE_SECONDS`) buys freshness with stop-loss latency. Maintenance runs
after `exits()` precisely so it cannot delay a stop check, and the cap exists because a
20-round-trip sweep at 45 s per call once delayed stops by tens of minutes and produced
two −99.99% exits. A larger cap moves back toward that failure.

**Not done automatically because** it trades a documented, previously-realised risk to
capital against an accuracy improvement, and the exchange rate is a judgement about how
much staleness matters — which the eleven-trade sample cannot settle either way.

---

## 9. A copy of the trading database is in this public repository's history

**Status:** stopped going forward. Removing it from history needs a decision.

`reset_account.py` and `unban_wallets.py` take a SQLite backup before they write,
named `<db>.<timestamp>.bak`. `.gitignore` covered `*.db` but not `*.bak`, so
`sentinel.db.1785082118.bak` was committed in `4a64c70` and pushed. The repository is
public (`visibility: public`, confirmed via the GitHub API).

What that file contains:

| | rows |
|---|---:|
| `wallet_watch` — the wallets being followed | 1190 |
| `wallet_blacklist` | 5568 |
| `paper_trades` | 23 |
| `engine_events` | 509 |

**No credentials.** Verified directly: `engine_state` holds only `last_cycle` and
`maint_sol`, and no table in the schema stores a token, key or secret.

**What it actually adds, measured against what this repo already publishes on purpose.**
`wallets-quality.txt` and `wallets-blacklist.txt` are committed deliberately — README
says to run the collector and commit the result — so the watch list is not secret by the
project's own design. The overlap is smaller than that makes it sound:

| | in the backup | already public | newly exposed |
|---|---:|---:|---:|
| watched wallets | 1190 | 343 | **847** |
| blacklist | 5568 | 500 | **5068** |
| trade-by-trade P&L | 23 | 0 | **23** |
| engine events | 509 | 0 | **509** |

So the incremental exposure is real — most of the current watch list, nearly the whole
blacklist, and the account's complete trading history — but "the watch list is the
strategy" overstates it, because a curated subset of that list is published by design.

**Fixed going forward:** `.gitignore` now covers `*.bak` and the `*-shm` / `*-wal`
sidecars (which `*.db-shm` never matched for a `.bak` file), and the file is untracked
via `git rm --cached` with the local copy left in place.

**Not done automatically:** the blob is still reachable in history. Removing it means a
`filter-branch`/`filter-repo` rewrite and a force-push, exactly as was done for the
leaked bot token. That was justified by a live credential; this is not one, and a
history rewrite invalidates every existing clone and checksum. Whether the watch list is
worth that is the operator's judgement, not mine.

**If you want it gone**, the same procedure as the token purge applies, and every branch
must be verified afterwards. Say so and I will run it.
