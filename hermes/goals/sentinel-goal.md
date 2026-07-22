# GOAL: Sentinel — Solana Smart-Money Copy-Trading Engine (from scratch)

Status: draft v2 — hand to Hermes agent as an overnight `/goal`
Scope: standalone project. Do NOT reuse Fasol, Agent-swarm, or the five-agent
orchestra pattern. No dependency on prior repos beyond what's listed here.
Constraint: **zero paid infrastructure**. Every data source and RPC path in
this doc must be reachable on a free tier or via raw open protocol. If a
step in this doc appears to require payment, stop and flag it — don't
substitute a paid shortcut without asking.

---

## 0. Pre-flight checklist (human does this BEFORE leaving the agent unattended)

- [ ] Sign up for free-tier accounts on **at least 3 of the 4 WS-capable
      providers** — Helius, Alchemy, QuickNode, GetBlock — since Section
      4.1 needs that many for the WS pool to have real failover. Ankr is
      a 5th, separate signup: optional, HTTP-only (no WebSocket), used
      for a different role (see 4.1) — it does not count toward the 3.
      So the realistic minimum is 3 signups (the WS four), 4 if you also
      add Ankr. Paste API keys into `.env` using these
      exact variable names (the agent's Phase-1 `.env.example` will match
      these — don't invent your own names, or the code won't find them):
      `HELIUS_API_KEY`, `ALCHEMY_API_KEY`, `ANKR_API_KEY`,
      `GETBLOCK_API_KEY`. QuickNode's free tier issues a full endpoint
      URL rather than a bare key — store that under `QUICKNODE_RPC_URL`.
      Optional, only if you also want Birdeye's cross-check pass (4.2):
      `BIRDEYE_API_KEY`. Leave unused providers' lines blank in `.env`
      rather than deleting them.
      Without this, the agent stalls at Phase 3 waiting for RPC access.
- [ ] Optional: provide a 5-10 address manual seed list in
      `discovery/seed_wallets.txt`. If skipped, explicitly tell the agent in
      the `/goal` invocation to proceed on automated discovery only
      (DexScreener + early-buyer reconstruction) — don't leave it silently
      blocked waiting for a list that isn't coming.
- [ ] Confirm the agent's runtime has outbound network access to: RPC
      endpoints, api.dexscreener.com, crates.io/pypi/npm registries (for
      `cargo add` / `pip install` during scaffold).
- [ ] Confirm `DRY_RUN` defaults to `true` in the config schema — this is
      about it being the *default*, not about keeping it that way forever;
      Section 8 completes the live path too, and that's fine, because the
      wallet the agent generates starts at 0 SOL and nothing in this
      project — not you, not the agent, not any script — sends it funds
      tonight. That's the actual safety mechanism, and it doesn't need you
      to do anything at signoff time, since there is no signoff step left.
- [ ] Do not send the 0.1 SOL anywhere near this project's wallet tonight.
      Not required by any step here — you already confirmed you won't —
      just stating it once more since it's the one manual action that
      would change the risk profile.

If all four mandatory items above are done (the seed list is the one
optional item), the rest of this doc runs unattended and at zero
cost — no live trades (nothing to fund them with), no paid API calls,
genuinely nothing else required from you. The agent doesn't stop to ask
anything; `PROGRESS.md` and whatever state the repo is in by morning —
not necessarily every phase finished, see Section 8's definition of done —
are the deliverable you'll find when you check in.

## 0.1 Runtime note: free/ephemeral coding-model backend

This `/goal` is executed by Hermes agent on OpenCode Zen's free DeepSeek V4
Flash. Per the operator's own observed numbers: compaction triggers at
~180K context, and the first message is already ~60K due to long-term
memory recall (crypto-trading history, prior account/skill creation). This
is a known, stable characteristic of this setup, not a risk to design
around — no special context-budget handling needed beyond normal good
practice.

One thing that stays true regardless of training-data policy on any given
model: sending a private key over a third-party API call is a larger
attack surface than not sending it, purely by number of systems that touch
it (network path, provider logging, provider-side incidents) — independent
of whether that specific provider trains on it. Given the amount at stake
here (0.1 SOL total budget), this is a low-stakes call and the operator's
choice to run fully transparent with the agent is reasonable — noting it
once for the record, not re-litigating it. The `PROGRESS.md` checkpoint
habit from below is still worth keeping regardless, since it's just good
practice for a long unattended session, not a defensive measure against
this specific model.

## 1. Mission

Build a self-hosted engine that:
1. Discovers candidate "smart" wallets from public on-chain data — no
   pre-existing list assumed.
2. Watches those wallets in near-real-time using only free-tier RPC access.
3. Scores and continuously re-scores them on edge quality, computed locally
   from raw transaction history — not borrowed from a third-party PnL API.
4. Selectively mirrors a subset of their trades with independent risk
   management — this is a filter, not a mirror.
5. Runs in **paper mode by default**, with the live-submit path fully
   built alongside it (see Section 8) — safe to complete because the
   trading wallet is never funded by the agent, from Fasol or any other
   source (Section 3). The wallet's zero balance, not a permission gate,
   is what keeps live execution inert until the human funds it.

## 2. Why this design (context for the agent, don't re-litigate)

- Naive copy-trading loses structurally: you execute after the source
  wallet, so you only capture the edge that survives one block (or more) of
  lag. Two ways to fight this: (a) minimize lag on the ingest→execute path,
  (b) select source wallets whose edge doesn't evaporate in one block
  (accumulators, not snipers). Build for both.
- Win-rate alone is a bad wallet filter — a wallet can have 70% win-rate and
  still be net-negative if losses are fat-tailed. Score on realized PnL
  distribution, not just hit-rate.
- **No free Yellowstone gRPC / ShredStream exists anywhere** (checked
  Helius, Alchemy, QuickNode, Chainstack, Triton, GetBlock — all gate
  streaming behind paid tiers, cheapest starts around $49/mo). This is a
  hard constraint, not a research gap. Accept the latency cost of free-tier
  WebSocket subscriptions for now (public RPC: ~100-200 rps/IP cap, 2-5s
  data delay on the truly public endpoint; free-tier keyed WS from
  Helius/Alchemy/etc. is push-based and faster than that, but still not
  gRPC-grade). Revisit paid gRPC only after Sections 8's phases prove the
  strategy has edge on free infra — don't front-load spend on unproven
  logic.
- Because free infra caps request volume, the architecture must be
  push-based (WebSocket `logsSubscribe`/`accountSubscribe`) wherever
  possible instead of polling. Polling burns request quota linearly with
  time; subscriptions don't.

## 3. Non-negotiable safety rules

- Never commit a private key, seed phrase, or `.env` to git. `.gitignore`
  must include `.env*`, `*.key`, `wallets/`.
- Never install a crate/npm package with a name that isn't on the allowlist
  below (Section 3.1) without flagging it to the human first and
  explaining why it's needed. Third-party "helper" packages are the single
  most common malware vector in this exact niche (documented supply-chain
  attacks via fake Solana bot repos, mid-2025 onward, with inflated
  stars/forks via sock puppet accounts) — treat any unfamiliar dependency
  claiming to help with "encoding," "layout," or "utils" for wallets/keys
  as hostile until proven otherwise.

### 3.1 Dependency allowlist

Pre-approved, no need to flag. Anything else — flag to the human first,
including the reason it's needed, before running `cargo add` / `pip
install` / `npm install`.

- **Rust**: `tokio`, `solana-sdk`, `solana-client`, `serde`, `serde_json`,
  `rusqlite`, `reqwest`, `bs58`, `anyhow`, `tracing` (or
  `log` + `env_logger`). Jito bundle submission: use Jito's documented
  Block Engine HTTP/gRPC endpoint directly (no unofficial "jito-helper"
  crates).
- **Python**: `pandas`, `numpy`, `requests`, `streamlit`, `python-dotenv`,
  `solders` or `solana-py` (keypair generation/signing only — verify it's
  the actual PyPI package from the official `michaelhly/solana-py` or
  `solders` maintainers, not a typo-squat), stdlib `sqlite3`.
- Anything touching key generation, signing, or encoding (base58, bip39,
  ed25519) is extra-high scrutiny regardless of whether it's on this list
  — prefer the crates/packages above over adding a new one for this
  purpose.
- Default `DRY_RUN=true`. Every execution path must check this flag before
  sending a real transaction. Log what *would* have been sent instead.
  `DRY_RUN` and the `LIVE` flag referenced in Section 8 are two
  **independent** booleans in `config.toml`, both defaulting to values
  that prevent sending (`DRY_RUN=true`, `LIVE=false`) — this is an
  intentional double-gate, not a naming inconsistency. A real
  `sendTransaction` call requires **both** `DRY_RUN=false` AND
  `LIVE=true`; if either one blocks it, take the DRY_RUN/log-only path.
  Don't collapse these into a single flag during implementation — the
  point of having two is that flipping one alone (e.g. a config typo)
  still isn't enough to send a real trade.
- Trading wallet must be a fresh, isolated keypair, generated by the agent
  as part of setup, never the main wallet. **This project must not access,
  reference, import, or interact with the Fasol wallet, Fasol's private
  keys, or the Fasol agent API in any way** — full isolation, no shared
  credentials, no cross-project fund movement. This holds regardless of
  `/yolo` or any autonomy setting; it is not a "wait and ask" rule (those
  can be overridden by an autonomy mode), it is a "this codebase has zero
  access to that system, full stop" rule — don't add a dependency, config
  reference, or RPC call that touches it.
- The agent never funds the trading wallet from any source under its
  control (no bridging, no touching an exchange account, no moving funds
  from anywhere). The wallet is expected to sit at 0 SOL for the entire
  build. This is intentional and is the actual safety mechanism for
  Section 8, not a limitation to work around.
- Position size hard cap in config, enforced in code (not just documented).
- Generated trading-wallet keypair is written to `wallets/trading_wallet.json`
  (already covered by `.gitignore`, see above) with file permissions set to
  `0600` at creation time. This file is the only copy — nothing else in
  this project persists the key. Note in `PROGRESS.md` that the human
  should back it up (copy off-box) before ever funding the wallet; losing
  it tonight costs nothing since it holds 0 SOL, but losing it after
  funding would be unrecoverable.
- Any reverse-engineered/unofficial API (see 4.2) is a soft dependency:
  code must degrade gracefully (log + skip, don't crash) if it goes down or
  starts returning garbage, since it can be revoked or IP-blocked without
  notice.

## 4. Stack decisions (final, don't relitigate mid-build)

| Layer | Choice | Why |
|---|---|---|
| Hot path (ingest → decode → build tx → submit) | **Rust** | Latency-critical path; every competitive implementation researched uses Rust here. Tokio async runtime. |
| Scoring / discovery / analytics / backtesting | **Python** | Not latency-critical, faster iteration, existing familiarity, pandas/numpy ecosystem. |
| Data feed | **Free-tier RPC pool**, WebSocket-first (see 4.1) | No paid option exists; see Section 2. |
| Execution | Direct instruction construction per-venue (Pump.fun, PumpSwap, Raydium CPMM/AMM v4) + **Jito bundles** for landing/tip | Jito bundle *submission* is free — you only pay the tip (in SOL) as part of trade economics, not an infra bill. Jupiter aggregation adds a routing round-trip; skip it on the hot path, keep as feature-flagged fallback only. |
| Inter-process comms | None needed as a separate service. The only cross-language hop is `scorer` (Python) → the Rust binary, and that's already covered by SQLite (`scorer` writes wallet tier, `filter` reads it — see Section 5) — no pub/sub required since a tier update every 15 min doesn't need push delivery. `ingest/`, `filter/`, `risk/`, `executor/`, `position_mgr/` are one Rust binary (single `cargo` package, internal modules communicating via direct function calls / in-process channels e.g. `tokio::sync::mpsc`) | Dropping Redis removes a dependency that has no actual job here once SQLite already carries the one cross-language signal — one fewer thing to install/run in the sandbox, one fewer thing that can silently not be running. |
| Persistence | SQLite for wallet stats/trade history | Single-node project, no need for more. |
| Config | `.env` + a single `config.toml` for strategy parameters — no hardcoded magic numbers in code | |

### 4.1 Free RPC pool (replaces paid gRPC)

Sign up for free tiers on **at least 3** of: Helius (free plan, includes
WS), Alchemy (free, 30M CU/month, Solana supported, includes WS),
QuickNode (free trial tier), GetBlock (free tier) — these four support
WebSocket subscriptions on their free tiers and are the ones that count
toward the **WS round-robin pool** the Rust `ingest/` module (Section 2's
push-based requirement) actually needs.

**Priority order within the WS pool (latency-driven, still $0)**: Helius
and Alchemy are primary — route the majority of subscription traffic to
whichever of the two is currently healthiest, since both are the fastest,
most Solana-mature free WS tiers available. QuickNode and GetBlock are
secondary — only take load when a primary key is rate-limited or down,
not round-robined evenly with the primaries by default. This is a
priority weighting, not exclusion: still register all keys you have so
there's failover capacity, but don't spread load evenly across all four
if it means idle Helius/Alchemy capacity while GetBlock/QuickNode carry
requests — that trades latency for no benefit at $0 cost either way.

**Ankr's free tier is HTTP-only —
no WebSocket, even on free public+** (WS is a paid-plan feature there).
Don't count it toward the WS pool or `ingest/` will silently end up with
fewer real WS sources than the "3 providers" count implies. Ankr is still
useful as a free plain-HTTP JSON-RPC endpoint for the *non-push* callers
— `discovery/` and `scorer/`'s `getSignaturesForAddress` /
`getTransaction` polling calls — so sign up for it for that role, not as
a WS pool member. Within the WS-capable pool, apply the priority
weighting above (not flat round-robin) so no single key's rps cap becomes
the bottleneck for the primaries specifically. Fall back to the fully public
`api.mainnet-beta.solana.com` endpoint only as a last resort (lowest
priority, expect 2-5s staleness). Implement exponential backoff (2s → 4s →
8s) on any 429, and log which key hit the limit so the pool can rebalance.

Primary subscription target: `logsSubscribe` on the known program IDs for
Pump.fun, PumpSwap, and Raydium (AMM v4 / CPMM), filtered client-side to
transactions touching tracked wallets. This is free, push-based, and
doesn't require any vendor's proprietary API — it's raw Solana RPC.

### 4.2 Free wallet-discovery data sources

- **DexScreener API** — no key required, 300 req/min on pairs endpoints, 60
  req/min on token-profile endpoints. Use it to find tokens with large
  recent gains (candidates for "who bought this early").
- **Birdeye free tier** — 30k compute units/month, includes a wallet
  PnL/win-rate endpoint. Budget is small — use it as a *batched, one-shot
  confirmation* pass on a short candidate list, not a continuous poll.
- **Manual seed list** — browse GMGN's public Discover/Smart Money tab and
  Birdeye's public wallet-tracker leaderboard in a normal browser, copy
  15-30 addresses that show consistent (not one-hit) performance. Zero API
  cost, and a human sanity-check on the seed list is genuinely valuable —
  don't skip this step in favor of pure automation.
- **Unofficial reverse-engineered Solscan API** (e.g. the pattern used by
  the `free-solscan-api` project — hits Solscan's internal website API, no
  key, no documented rate limit) — optional bonus source only. It's not
  ToS-sanctioned, can break or get IP-blocked without warning, and must
  never be a load-bearing dependency (see 3's degrade-gracefully rule).

## 5. Module breakdown

Process boundaries: `discovery/`, `scorer/`, `telemetry/` are separate
Python processes (run as scheduled/periodic jobs, not long-running
servers). `ingest/`, `filter/`, `risk/`, `executor/`, `position_mgr/` are
one Rust binary (single `cargo` package, internal modules) — this is the
always-running hot-path process. The only cross-language hop is
`scorer` → the Rust binary, and it goes through SQLite (`scorer` writes
wallet tier, `filter` reads it, below) — no separate message broker.
`filter/` should cache the tier table in memory and refresh it on an
interval (config value, e.g. every 30s) rather than hitting SQLite on
every incoming `SwapEvent`, so the read doesn't sit on the hot path.

```
sentinel/
├── discovery/      (Python) implements 4.2 — pulls trending/top-gainer
│                  tokens from DexScreener, walks early-buyer history for
│                  each via free RPC getSignaturesForAddress, surfaces
│                  wallets that appear as early buyers across multiple
│                  winning tokens; merges with the manual seed list;
│                  outputs a candidate wallet list to SQLite
├── ingest/         (Rust) WebSocket subscriptions against the free RPC
│                  pool (4.1), filters to tracked wallet list, decodes
│                  swap instructions per-venue into a normalized SwapEvent
├── scorer/         (Python) periodic job — for each tracked wallet, pulls
│                  raw tx history via free RPC (getSignaturesForAddress +
│                  getTransaction), parses swap instructions LOCALLY
│                  (Pump.fun/PumpSwap/Raydium) to reconstruct buys/sells
│                  and compute realized PnL itself — no dependency on any
│                  third party's PnL number surviving long-term. Birdeye
│                  (4.2) used only as a sparing cross-check. Computes the
│                  scoring model (Section 6), writes wallet tier to SQLite
├── filter/         (Rust) consumes SwapEvent + current wallet tier from
│                  SQLite, decides copy/skip based on strategy config
├── risk/           (Rust) position sizing, exposure caps, token security
│                  pre-check (LP lock, mint authority, freeze authority,
│                  top-10 holder %) before allowing a buy
├── executor/       (Rust) builds venue-specific instruction, wraps in Jito
│                  bundle w/ tip, submits; DRY_RUN short-circuits before
│                  submit and logs instead
├── position_mgr/   (Rust) tracks open positions, applies TP/SL/trailing
│                  stop, triggers auto-sell through executor
├── telemetry/      (Python) reads SQLite trade log, produces daily report:
│                  per-wallet contribution, realized PnL, slippage vs.
│                  source wallet's fill, false-positive rate on security
│                  pre-checks, and free-tier quota usage (so the human can
│                  see how close the pool is to its limits)
└── dashboard/      (Python, Streamlit) READ-ONLY local web UI over the
                   same SQLite file — tracked wallets + current tier,
                   discovered/watched tokens, paper-account balance, open
                   positions, trade history feed. No write path to SQLite,
                   no control over the engine (no start/stop/buy/sell
                   buttons) — this is a window, not a cockpit, so a UI bug
                   can never affect execution. Lowest priority module,
                   build last, doesn't block any other phase.
```

## 6. Wallet scoring model (concrete, implement exactly this — v1)

Recomputed every 15 minutes per tracked wallet (config value) — not on
every trade, to stay within free-tier RPC quota. For each tracked wallet,
over a rolling 14-day window, computed from the locally-parsed
transaction history (see `scorer/` above, not a third-party PnL figure):

- `payoff_ratio = avg(win_size) / avg(loss_size)` — not just win-rate.
- `edge_score = win_rate * payoff_ratio - (1 - win_rate)` — expectancy per
  trade in R-multiples. Reject wallets with `edge_score <= 0`.
- `activity_filter`: exclude wallets with >300 tx/week (likely bots/spam,
  not discretionary smart money) and <5 tx/week (insufficient sample).
- `recency_decay`: weight last-7-days trades 2x vs. days 8-14. Edge decays;
  a wallet that was good a month ago may already be arbed out or copied to
  death by others.
- `cluster_check`: flag wallets whose buy timestamps correlate >90% with
  another already-tracked wallet (likely same entity/multisig) — don't
  double-count correlated wallets as diversification.
- Output: tier `A` (edge_score top quartile, auto-copy eligible), `B`
  (watch-only, log but don't copy), `C` (drop from tracking).

This whole module is intentionally simple in v1. Do not add ML/Bayesian
bandit logic yet — get the deterministic version working and logging real
data first. (Thompson sampling over wallet selection is a natural v2 once
there's a trade history to learn from — note this in code comments as
future work, don't build it now.)

## 6.1 Realistic paper-trading fill model (required, not a nice-to-have)

The operator wants paper/demo trades that account for everything a real
trade would cost, so the resulting win-rate and PnL numbers are actually
trustworthy — not an optimistic simulation that assumes perfect fills.
A naive paper trader (assume you get the source wallet's exact price,
zero fees) will always look more profitable than reality and defeats the
purpose of testing the strategy before ever funding it. Every simulated
fill in DRY_RUN mode must subtract/model, not ignore:

- **Execution lag**: don't fill at the source wallet's price. Fill at the
  pool's price N slots later (config value, default 2 slots / ~800ms,
  tune this once real ingest-to-decision latency is measured in Phase 3)
  — this is the honest version of Section 2's adverse-selection point,
  applied to the backtest itself, not just the live design.
- **Price impact / slippage**: compute the simulated fill price from
  actual pool state at the simulated fill slot using the venue's real
  pricing formula (Pump.fun bonding curve math, or constant-product for
  Raydium/PumpSwap AMM pools) given your configured position size — read
  via `getAccountInfo` against the free RPC pool, no funded wallet needed.
- **DEX swap fee**: apply the venue's actual fee in bps to the simulated
  trade size.
- **Network cost**: subtract an assumed priority-fee + Jito-tip cost per
  trade (config value, e.g. default 0.0005-0.002 SOL — document this as
  an assumption, since there's no live data to calibrate it from yet) from
  simulated PnL on both entry and exit.
- Store all of the above per-trade in the SQLite trade log (not just net
  PnL) so `telemetry/` can break down how much of the "edge" survives
  after lag+fees+slippage vs. how much was theoretical — this number is
  the actual answer to "should I fund this wallet," more than raw PnL is.

## 7. Risk management (hard rules, not suggestions)

- Max concurrent open positions: config value, default 5.
- Max allocation per position: config value, default 2% of trading wallet
  balance.
- Max allocation per source wallet (sum of open positions copied from one
  wallet): default 6% — prevents one bad smart from blowing up the account.
- Stop-loss: default -25%, trailing stop activates at +40% and trails
  15% behind the position's peak price (all three config values).
- Security pre-check before any buy: LP burned/locked, mint authority
  renounced, **freeze authority renounced** (this is the actual
  honeypot heuristic referenced in Section 5's module breakdown — an
  active freeze authority means the issuer can freeze any holder's token
  account, including yours, blocking sells entirely; check both
  authorities via `getAccountInfo` on the mint, not just mint authority),
  top-10 holder concentration < 40%. Fail any check → skip the
  trade, log why.

## 8. Build phases — do these in order, don't parallelize

**Definition of done, every phase**: `cargo build` (Rust modules) or the
Python module importing/running without error is required before a phase
is logged as complete in `PROGRESS.md`. If a phase is genuinely blocked
or only partially working by the time it's checkpointed, log it as
partial/blocked with the specific reason — don't mark it done to keep
moving. A shorter, honestly-annotated repo in the morning is a better
outcome than a longer one where some phases silently don't actually work.

**Soft time-box**: rough per-phase budget, since phases are sequential
and one stuck phase otherwise blocks all the cheaper ones after it —
1-2 ≈2.5h combined, 3 ≈1.5h, 4 ≈1.5h, 5 ≈1h, 6 ≈4h (hardest phase —
three venues' instruction encoding — most likely to run long), 7-8 ≈3h,
9 ≈1h or skip. These are soft: if a phase is going well, keep going past
its budget. But if a phase is stuck in a retry loop near or past its
budget with no real progress, stop, mark it partial/`UNVERIFIED` with
what's blocking it in `PROGRESS.md`, and move to the next phase rather
than spending the rest of the night on one module while the rest of the
build never starts.

1. **Scaffold**: repo structure above, `.gitignore`, `config.toml` schema,
   `.env.example` (no real values — must use the exact variable names
   pinned in Section 0's pre-flight checklist, since the human already
   populated real `.env` under those names before this session started),
   README documenting DRY_RUN behavior and
   the free-tier-pool constraint.
2. **Discovery**: implement `discovery/` against DexScreener (no key
   needed to start) + free RPC. Produce a candidate list of 30-100 wallet
   addresses with basic stats attached. Merge with a manually-curated seed
   list (human provides this — see Section 9). Validate output by manually
   spot-checking 5-10 addresses on a block explorer before trusting it.
3. **Ingest (paper mode only)**: connect the free RPC pool, subscribe to
   the candidate list from step 2, decode SwapEvents, print to log. Watch
   the pool's rate-limit logs — if you're hitting 429s constantly, that's
   a sign to add more free-tier keys or narrow the tracked list, not to
   reach for a paid endpoint. Validate stability over a few hundred events.
4. **Scorer**: implement Section 6 against locally-parsed tx history.
   Validate tiering output makes sense on a handful of manually-inspected
   wallets before trusting it.
5. **Filter + risk (still DRY_RUN)**: wire filter/risk to consume scorer
   output and ingest events, log "would copy" decisions with full
   reasoning (tier, edge_score, risk checks passed/failed). This is the
   artifact to review before ever touching live execution.
6. **Executor**: build real venue-specific instructions. Do NOT rely on
   RPC `simulateTransaction` for fill estimation — the trading wallet has
   0 SOL, and simulation still checks fee-payer balance against live chain
   state, so it will error on an empty wallet rather than return a useful
   quote. Instead, estimate fills using the local pricing math in Section
   6.1 (bonding-curve / constant-product formulas against live pool state
   read via `getAccountInfo` — a read-only call, doesn't need any balance).
   Complete the full live-submit code path too (Jito bundle construction,
   tip logic, actual `sendTransaction` call gated behind `LIVE`) — it's
   safe to finish this tonight since Section 3's unfunded-wallet rule
   means there's nothing for it to actually execute against yet.
   **Correctness note**: exact instruction layout for Pump.fun/PumpSwap/
   Raydium is the highest-risk part of this build to get subtly wrong. If
   you cannot verify an instruction's account ordering or discriminator
   against the venue's actual on-chain program (e.g. by cross-checking a
   real recent transaction for that program via `getTransaction` and
   comparing account/data layout), do not ship a best-guess encoding
   silently. Mark that venue's executor path as `UNVERIFIED` in code
   comments and in `PROGRESS.md` with what specifically couldn't be
   confirmed. An honestly-incomplete venue is fine to leave for tomorrow;
   a confidently-wrong one that passes DRY_RUN logging but would fail (or
   worse, silently misfire) on a real send is the actual danger here.
7. **Position manager + telemetry**: TP/SL/trailing logic against paper
   positions, daily report generation including free-tier quota usage.
8. **Live-submit path**: complete this as a normal build task — the
   `LIVE=true` code path, real `sendTransaction`, error handling for a
   rejected/failed send. This is safe to finish tonight because Section
   3's rules hold regardless of `/yolo`: the wallet is never funded by the
   agent, and the codebase never touches Fasol's wallet or keys. An
   attempted live send against a 0-SOL wallet will simply fail on
   insufficient balance for the network fee — there's nothing to lose, so
   there's no reason to gate this behind a human review step tonight.
   Deliverable by morning: both paper and live paths fully wired, wallet
   still at 0 SOL, decision on whether to fund it deferred to the human's
   review the next day — not something this session decides or asks about.
9. **Dashboard (anytime after Phase 4, doesn't block 5-8)**: build
   `dashboard/` as a Streamlit app reading directly from the SQLite file —
   tracked wallets/tiers, watched tokens, paper balance, open positions,
   trade feed. Strictly read-only against the DB; no engine control
   surface. If time runs short overnight, this is the module to skip or
   leave partial — it has zero bearing on safety or correctness of the
   trading logic.

## 9. What the agent should ask the human, vs. decide alone

With `/yolo` (or equivalent no-permission-needed mode) active, the agent
should not stall waiting on anything in this section — make a reasonable
default choice and log it, rather than blocking:

- RPC provider choice: pick from whichever free-tier keys are already in
  `.env`; if none are populated yet, note it in `PROGRESS.md` and skip to
  whatever discovery/build work doesn't need them yet, don't idle.
- Manual seed list: if `discovery/seed_wallets.txt` is empty, proceed on
  automated discovery only (Section 4.2) — don't wait for one to appear.
- Any other build/architecture judgment call within Sections 1-7: decide
  per the defaults already specified in this doc.

The one item that is never a default-and-proceed decision, `/yolo` or not,
is **funding and live-executing a real trade**. Building the live-submit
code path in Section 8 is in scope tonight and should be completed like
any other phase — what's never on the table, regardless of autonomy mode,
is the agent moving real SOL into the trading wallet or treating a
successful `LIVE=true` build as license to go find funds for it. That
boundary isn't "ask the human," it's "not something this session does,"
which is the version of this instruction that's actually robust to an
autonomy mode telling the agent to stop asking permission.
