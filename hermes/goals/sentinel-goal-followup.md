# GOAL: Sentinel — reconcile status, then resume real build (continuation, not a new project)

This continues the existing `~/sentinel` repo (commit ba6262e). Do not
restart or rewrite what's already there. The original `/goal` doc's rules
still apply in full (Fasol isolation, unfunded wallet, double-gate
DRY_RUN+LIVE, dependency allowlist, no secrets in prompts, PROGRESS.md
checkpointing) — this doc only adds a correction and a resume point.

## 0. Why this doc exists

Your last summary in chat claimed Phases 4, 5, 7, 9 complete and Phase 6
"coded but unverified." The actual `PROGRESS.md` committed to git shows
Phase 4 in progress and Phases 5-9 not started. That's a real discrepancy,
not a misunderstanding on the human's side — the two records disagree and
only one can be true. Before writing any new code, resolve this:

1. Run `git status` and `git log` in `~/sentinel`. Check whether Phases
   4-9 exist as uncommitted local changes, exist partially, or genuinely
   don't exist yet.
2. Open and actually read the current contents of `scorer/`, `src/`
   (filter/risk/executor/position_mgr modules), and `dashboard/` — don't
   infer their state from memory of the session.
3. Rewrite `PROGRESS.md` to match what you just verified by reading files,
   not what you recall deciding to do. If something is half-built, mark it
   half-built with the specific missing piece named.
4. Report the *reconciled* state back in your next message to the human —
   plainly say if the previous summary was wrong and by how much. Don't
   soften this; an inflated status report is the specific failure being
   corrected here.

**Going forward**: never mark a phase complete in `PROGRESS.md` or in a
chat summary without having just read the relevant file(s) in that same
turn. "I built this earlier in the session" is not verification — files
change, sessions compact, memory of what you did is not the same as what's
on disk. Read-then-report, every time, for the rest of this build and
future ones.

## 1. Free-tier account creation — you're authorized to do this yourself

The human is stepping back from manual setup. You have handled autonomous
account creation before, so: sign up for free-tier accounts yourself on at
least 3 of Helius, Alchemy, QuickNode, GetBlock (WS-capable, per Section
4.1 of the original doc) and Ankr (HTTP-only role). Use a dedicated
email/identity for this project if you're able to generate one, rather
than reusing credentials from Fasol or elsewhere — keeps this project's
accounts cleanly separated the same way the wallet already is. Populate
`.env` with the resulting keys using the exact variable names from the
original doc's Section 0 (`HELIUS_API_KEY`, `ALCHEMY_API_KEY`, etc.).
Log which providers you actually signed up for in `PROGRESS.md` (not the
credentials themselves — just "Helius: signed up, key in .env" style
confirmation) so the human has a record of what accounts exist under this
project when they check in later.

If a provider's signup requires something you can't complete autonomously
(phone verification, payment method on file even for a free tier, a
CAPTCHA you can't solve), don't get stuck retrying — log it as blocked in
`PROGRESS.md` with the specific blocker, move to the next provider, and
proceed once you have the minimum 3 WS-capable keys rather than all 4.

## 2. Resume the actual build from the reconciled state

Once Section 0's reconciliation is done and Section 1's keys are in
`.env`:

- If Phase 3 (Ingest) was never validated against real WS traffic, do that
  now that keys exist — this was the original blocker.
- Continue Phase 4 (Scorer) from wherever it genuinely stands, through
  Phases 5-9, in the order and with the correctness bar defined in the
  original `/goal` doc — including the Phase 6 instruction-encoding
  verification requirement (cross-check against real on-chain transactions
  before marking a venue's executor path anything other than `UNVERIFIED`)
  and the Phase 8 double-gate (wallet stays unfunded; this doesn't change).
- Keep using the soft time-box guidance from the original doc's Section 8
  preamble, and keep marking anything genuinely incomplete as incomplete —
  the whole point of this follow-up is that an honest partial state beats
  a confident wrong one.

Work autonomously through this without asking the human anything — same
`/yolo`-compatible posture as the original doc, same hard boundary that
funding/live-executing a real trade is never in scope regardless of how
confident the build looks.
