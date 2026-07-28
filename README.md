# Sentinel: GMGN weighted-convergence paper trading

The runtime path is intentionally small:

```text
GMGN Smart Money feed -> 7d activity + 30d win rate -> weighted convergence
-> paper wallet (0.1 SOL) -> Telegram-readable journal
```

## Strategy

A wallet is eligible when it traded during the last 7 days and its 30-day win rate is at least 50%. Signal weights (`paper_engine.WEIGHT_TIERS`, the single source — the panel and the bot build their buckets from it):

| 30d win rate | Weight | Against `ENTRY_SCORE=1.0` |
|---|---:|---|
| 90% to 100% | 1.0 | enters on its own |
| 80% to <90% | 0.5 | two of them enter |
| 70% to <80% | 0.25 | four of them enter |
| 60% to <70% | 0.0625 | contributes only |
| 50% to <60% | 0.03125 | contributes only |

A token entry requires the weighted score to reach `GMGN_ENTRY_SCORE` (default 1.0) inside a 30-minute window. Existing positions are never re-entered; a closed token has a cooldown. This is a ranking heuristic, not a probability claim.

The ladder and the threshold only mean something read together. An earlier pairing put a 0.25 top tier against a 0.25 threshold, so a single 70% wallet was already a full signal and "weighted convergence" never converged — every entry fired on one wallet.

Weights are read from the win rates already cached in `wallet_watch`, so the poll loop makes one feed call rather than twenty stats calls. Wallet bookkeeping (refresh, discovery, cleanup) runs on its own `GMGN_MAINTENANCE_SECONDS` timer, after the stop-loss check — never before it.

## Mass wallet discovery

`wallets-quality.txt` is a generated snapshot, not a hand-maintained truth source. The current committed snapshot is only a small sample. To rebuild it from current GMGN data and target up to 3,000 verified Solana wallets:

```bash
# check the API key first, then run from repository root
gmgn-cli config --check
python gmgn/mass_discovery.py --target 3000 --max-tokens 300 --min-winrate 0.50 --min-7d-trades 1 --min-30d-trades 5
```

The collector combines `track smartmoney`, `track kol`, trending and trench tokens, and `token traders`, then verifies candidates through `portfolio stats`. It writes atomically and never calls swap.

Three gates are applied, with these defaults:

| Gate | Flag | Default |
|---|---|---:|
| 30-day win rate | `--min-winrate` | 0.50 |
| 30-day sample size | `--min-30d-trades` | 3 |
| 7-day activity | `--min-7d-trades` | 0 — **off** |

The 7-day gate is disabled by default: GMGN's activity fields are sparsely populated, and requiring them dropped most otherwise-qualified wallets. Pass `--min-7d-trades 1` to enforce it. Credentials come from the repo-local `.env`, the same as the engine.

For a cheap preview:

```bash
python gmgn/mass_discovery.py --dry-run --target 3000 --max-tokens 30
```

Do not commit a generated multi-thousand-wallet snapshot blindly. Run it with the connected GMGN account, inspect the count and top rows, then commit the resulting `wallets-quality.txt`. The bot can keep running during discovery because the file replacement is atomic.

## Scaling to tens of thousands: pump.fun harvesting

GMGN's curated feeds are the bottleneck for pool size, not for quality — `track smartmoney` and `track kol` together surface a few hundred wallets, and the weighted rule needs a far bigger universe before several qualified wallets ever land on the same fresh token.

Since this project only trades the pump.fun launchpad (and PumpSwap), pump.fun's public `/coins` API is the natural mint-discovery source: it surfaces every active and graduated coin on the launchpad. For the wallet addresses themselves, the pipeline uses **GMGN's `token traders` API** — the same endpoint the engine already calls — because pump.fun's own `/trades/all/{mint}` endpoint was deprecated (returns 404 as of 2026-07).

**pump.fun does not publish win rates.** There is no PnL or "smart wallet" endpoint; anything advertising one is a third-party wrapper. So the pipeline is two stages and the second one is the expensive half:

```text
stage 1  pump.fun /coins -> mint list -> GMGN token traders -> wallet addresses  (one GMGN call per mint)
stage 2  GMGN portfolio stats -> 30d win rate + sample size                     (rate-limited bulk)
```

```bash
# one-off: harvest 300 mints, verify the 2,000 most promising wallets
python gmgn/pumpfun_discovery.py --harvest-mints 300 --verify 2000

# continuous: keep growing the pool in the background
python gmgn/pumpfun_discovery.py --loop --harvest-mints 150 --verify 1000

# counters only, no API calls
python gmgn/pumpfun_discovery.py --status
```

Verified wallets are written straight into `wallet_watch` with source `pumpfun`, so the engine picks them up on its next maintenance tick without a restart, and mirrored to `wallets-pumpfun.txt` for inspection.

Both stages are resumable: scanned mints, candidate stats and verification status live in SQLite (`pumpfun_candidates`, `pumpfun_scanned_mints`, `pumpfun_wallet_mints`). Stop it with Ctrl+C and the next run continues instead of restarting.

Two design details that matter more than they look:

- **Verification order is by distinct mints, then volume.** Stage 2 is the scarce resource, so it is spent on wallets that traded several different coins — repeat traders — rather than on the pool in insertion order.
- **Missing stats are not a rejection.** A wallet GMGN returned no row for stays `new` and is retried later. Only a confirmed sub-50% win rate, or too small a 30-day sample, marks it `rejected`.

Expect the funnel to be brutal, and that is the point: most launchpad traders lose money, so tens of thousands of harvested addresses will yield a much smaller verified set. Growing the pool is cheap; the honest limit is how fast GMGN's rate limiter lets stage 2 confirm win rates.

| Setting | Default | Purpose |
|---|---:|---|
| `PUMPFUN_TRADERS_LIMIT` | 100 | traders per mint from GMGN |
| `PUMPFUN_MIN_MINTS` | 2 | distinct coins before a wallet is worth verifying |
| `PUMPFUN_MIN_TRADES` | 3 | harvested trades before a wallet is worth verifying |
| `PUMPFUN_MIN_WINRATE` | 0.50 | GMGN 30d win-rate gate |
| `PUMPFUN_MIN_30D_TRADES` | 5 | reject lucky 100%-on-one-trade wallets |
| `PUMPFUN_STATS_DELAY` | 0.35 | pause between GMGN stats batches |
| `PUMPFUN_RECHECK_SECONDS` | 86400 | re-verify an accepted wallet after a day |
| `PUMPFUN_AUTH_TOKEN` | (unset) | optional pump.fun JWT for fewer throttles |

## Paper account and exits

- Initial paper balance: 0.1 SOL.
- Stake per entry: 0.025 SOL.
- Trailing stop: +25% activation, 15% trail.
- Emergency hard stop: -45%, full-position exit.
- Max holding time defaults to 1h (`GMGN_MAX_HOLD_SECONDS`).
- UTC timestamps and PnL are stored in SQLite and exposed through Telegram.

## Configuration

Every credential and tunable lives in one gitignored `.env` at the repository root; nothing needs to be exported into the shell.

```bash
cp .env.example .env                  # then fill in the values
python gmgn/config.py                 # print resolved config, secrets masked
python gmgn/config.py --import-gmgn   # move ~/.config/gmgn/.env keys into the project
```

Precedence is process environment > `.env` > built-in default, so a one-off `GMGN_ENTRY_SCORE=0.4 python ...` still wins.

## Run

```bash
npm install -g gmgn-cli
python gmgn/supervisor.py
```

That starts the engine, the Telegram bot and the Mini App server together and restarts any of them that exits — a stopped engine is the expensive failure here, because open positions are not checked against their stops while it is down. Use `--no-bot` / `--no-webapp` / `--no-engine` to run a subset, or start the pieces individually with `python gmgn/run_engine.py`, `python gmgn/telegram_bot.py`, `python gmgn/webapp.py`.

Telegram commands: `/status`, `/positions`, `/trades`, `/wallets`, `/weights`, `/attribution`, `/config`, `/help`.

## Telegram Mini App

`python gmgn/webapp.py` serves a panel at <http://127.0.0.1:8770> with equity curve, live position P&L, trade history, wallet P&L attribution, the wallet pool and tokens approaching the entry threshold. It works in a plain browser as-is, so long as there is no public origin — once there is, signing is required and a plain browser gets the panel's «нет доступа» screen rather than data.

Telegram only opens Mini Apps over HTTPS, so the panel needs a public origin:

```bash
python gmgn/supervisor.py --tunnel
```

That publishes the panel, hands the URL to the bot, and installs it as the chat's menu button and as a keyboard button. The URL is issued fresh on each connect, so nothing needs pinning in `.env` — when it changes, the bot is restarted with the new one.

Two providers, tried in order (`TUNNEL_PROVIDER` = `auto` | `cloudflared` | `pinggy`):

| Provider | Requires | Notes |
|---|---|---|
| `cloudflared` | `npm install -g cloudflared` | Needs outbound 7844. Some networks complete the TCP connect but reset the TLS handshake to the edge (`TLS handshake with edge error: EOF`) — those cannot use it, whichever `TUNNEL_PROTOCOLS` value you pick. |
| `pinggy` | `ssh` (already present) | Rides SSH on 443, so it works where the above is filtered. Free sessions expire after 60 minutes and reconnect with a new hostname. |

If you already have a domain or VPS, skip the tunnel and set `WEBAPP_PUBLIC_URL` in `.env` instead.

Either way, a public origin turns on request signing: the API then requires valid Telegram `initData`, checked by HMAC against the bot token and pinned to `TELEGRAM_CHAT_ID`, so nobody else can read the panel through the tunnel.

The API is read-only (`/api/overview`, `/api/trades`, `/api/wallets`, `/api/weights`, `/api/attribution`, `/api/events`, `/api/equity`, `/api/health`) and opens the database in SQLite read-only mode. `/api/health` is the one endpoint exempt from signing — the tunnel probes it to confirm the origin really serves before the URL is handed out.

## Tests

```bash
python -m unittest discover -s gmgn -p 'test_*.py'
```

The supplied 16 Solana wallets are in `data/seed_wallets_sol.txt`. The separate EVM CSV is not mixed into Solana data.

## Paper-only, enforced rather than asserted

The runtime never signs anything, submits a swap, or moves SOL. That is not left to
the code being careful: `config.gmgn_env()` strips `GMGN_PRIVATE_KEY` from the
environment handed to every `gmgn-cli` subprocess, including one inherited from the
ambient shell. Signing capability is absent from the child process, so a bug that
tried to submit a transaction would fail for lack of a key rather than succeed.

Every call the engine makes — smart-money feed, portfolio stats, token info, KOL
discovery — was verified to work with the key withheld. `gmgn_env(allow_signing=True)`
exists so that a caller which genuinely needs to sign has to say so where it can be
reviewed; a test asserts no file in this project does.

The pump.fun harvester follows the same rule: it only performs public GET requests
against the launchpad and read-only `portfolio stats` calls through the same
key-stripped environment.
