# Sentinel — GMGN Weighted Paper Engine

Solana copy-trading paper engine. Tracks smart-money wallets via
`gmgn-cli`, clusters their trades by token, enters paper positions when
weighted score ≥ ENTRY_SCORE.

## Quick start

```bash
# 1. Activate env
cd ~/sentinel
source .venv/Scripts/activate  # or: source .venv/bin/activate

# 2. Run engine (reads .env automatically)
python gmgn/run_engine.py
# Or with overrides:
# GMGN_CHAINS="sol" GMGN_ENTRY_SCORE="1.0" python gmgn/run_engine.py

# 3. Run Telegram bot (separate terminal)
python gmgn/telegram_bot.py

# Or start everything (engine + bot + mini app) at once:
python gmgn/supervisor.py
```

## Environment

**Never put a real secret in a tracked file.** All credentials live in `.env`
at the repo root, which is gitignored. `gmgn/config.py` loads it automatically
for every process — no `export` needed.

```bash
cp .env.example .env   # then fill in the values
```

## Dependencies

| Tool | How to install |
|---|---|
| `gmgn-cli` (v1.5.2+) | `npm install -g gmgn-cli` |
| Python 3.11+ | bundled |
| SQLite3 | bundled |

Python packages: none beyond stdlib (`sqlite3`, `urllib`, `json`, etc.)

## Architecture

```
                    gmgn/config.py  ← repo-local .env (all credentials)
                           │
gmgn-cli ──REST──► gmgn/paper_engine.py ──SQLite──┬─► gmgn/telegram_bot.py
 (read-only            │                          │     commands + push
  GMGN API)      weighted convergence             │
                 + paper account                  └─► gmgn/webapp.py
                 + trailing/hard stops                  read-only JSON API
                 + wallet discovery                     + Mini App UI

                 gmgn/supervisor.py — runs all three, restarts on exit
```

`gmgn/config.py` is the single source of truth for settings and secrets. Add new
tunables there rather than calling `os.getenv` at a use site, and document each one
in `.env.example` — three tests enforce that the two stay in step, including that
documented defaults match the literals in the code.

## Paper-only is enforced, not assumed

`config.gmgn_env()` strips `GMGN_PRIVATE_KEY` before spawning `gmgn-cli`, so the
subprocess cannot sign a swap even if asked. Every read the engine performs was
verified to work without it. `gmgn_env(allow_signing=True)` is the deliberate
opt-in; a test asserts no file here uses it.

## Cycle ordering (do not reorder)

`cycle()` runs: feed → **exits** → entries → throttled maintenance. Stop-loss checks
must stay ahead of anything that calls the stats API. They used to run after a
`get_stats()` sweep of every maker in the feed (up to 20 round-trips × 45 s timeout),
which delayed stops by tens of minutes and produced two −99.99% exits on a −45% hard
stop. `test_exits_run_before_wallet_stats` guards this.

Entry weights come from `cached_winrates()` — the win rates already in `wallet_watch` —
so the fast path costs one API call. `refresh_wallet_stats` keeps those values current
in the background, bounded by `GMGN_STATS_BATCH_MAX`.

## Key parameters

| Env var | Default | Purpose |
|---|---|---|
| `GMGN_CHAINS` | `sol` | Chains to poll (only `sol` works) |
| `GMGN_POLL_SECONDS` | 15 | Feed poll interval |
| `PAPER_BUDGET_SOL` | 0.1 | Initial paper balance |
| `PAPER_TRADE_SIZE_SOL` | 0.025 | Stake per entry |
| `GMGN_CLUSTER_WINDOW_SECONDS` | 1800 | 30 min cluster window |
| `HARD_STOP_PCT` | 45 | Emergency exit level |
| `GMGN_MAX_HOLD_SECONDS` | 3600 | Auto-close after 1 h |
| `GMGN_ENTRY_SCORE` | 1.0 | One 90%+ wallet, or two 80-90%, or four 70-80% |
| `GMGN_MAINTENANCE_SECONDS` | 600 | Wallet bookkeeping interval |
| `GMGN_STATS_BATCH_MAX` | 6 | Cap on stats round-trips per pass |
| `WEBAPP_PORT` | 8770 | Mini App server port |
| `WEBAPP_PUBLIC_URL` | — | HTTPS origin; enables the Telegram button **and** auth |

## Weight computation

`paper_engine.WEIGHT_TIERS` is the single source; the panel and the bot build their
win-rate buckets from it, and `MIN_WEIGHTED_WINRATE` / `TOP_WINRATE` derive from it so
no threshold literal exists anywhere else.

| 30d win rate | Weight | Against `ENTRY_SCORE=1.0` |
|---|---:|---|
| 90–100% | 1.0 | enters on its own |
| 80–90% | 0.5 | two of them enter |
| 70–80% | 0.25 | four of them enter |
| 60–70% | 0.0625 | contributes only |
| 50–60% | 0.03125 | contributes only |
| below 50% | 0 | blacklisted by `cleanup_wallets` |

The ladder and the threshold only mean something read together. The earlier pairing
was a 0.25 top tier against a 0.25 threshold, so one 70% wallet was a full signal and
"weighted convergence" never converged — every entry fired on `wallets=1`.

## Attribution

`trade_wallets` records which wallets produced each entry and what each contributed.
`wallet_attribution()` splits every closed trade's P&L across them by weight share, so
a lone 90% wallet owns its whole result and one of four owns a quarter. Surfaced as
`/attribution` and the panel's «Вклад» tab. The totals reconcile exactly with realised
P&L — a test asserts it.

## Known issues

- **`wallet_stats` API** (`portfolio stats` endpoint) intermittently times out
  (ConnectTimeoutError). The engine skips timeouts and works with available data.
- **`token_price` nested dict** — GMGN returns `price` as `{"address":..., "price":"0.0000013"}`
  instead of a scalar. Already fixed in `token_price()` — check if any new
  price-parsing code hits the same trap.
- **Windows PATH** — `gmgn-cli.cmd` extension is required for Python
  `CreateProcess` on Windows; `_find_gmgn()` handles it, falling back to
  `%APPDATA%\npm`.
- **cp1251 console** — logs and Telegram text are Russian with emoji. Every
  entrypoint calls `config.use_utf8_stdio()` first; without it the first such
  line raises `UnicodeEncodeError` and kills the process.

## Telegram bot commands

| Command | Description |
|---|---|
| `/status` | Balance, realized P&L, engine heartbeat, open positions |
| `/positions` | Per-position entry/peak/stop/expiry |
| `/trades` | Last 12 trades |
| `/wallets` | Wallet pool by win-rate bucket |
| `/weights` | Tokens near the entry threshold |
| `/attribution` | Realised P&L split across the wallets that triggered each entry |
| `/config` | Resolved engine parameters |
| `/help` | This menu |

## Mini App

`gmgn/webapp.py` + `gmgn/webapp/index.html`. Read-only JSON API over SQLite
(`mode=ro`) plus a single-page UI. Auth is Telegram `initData` HMAC, required
whenever `WEBAPP_PUBLIC_URL` is set; the signature is pinned to
`TELEGRAM_CHAT_ID` so only the owner can read the panel through a tunnel.

`gmgn/tunnel.py` publishes it over HTTPS (`supervisor.py --tunnel`). **This
network cannot use cloudflared** — TCP to `region1.v2.argotunnel.com:7844`
connects, but the TLS handshake to the edge is reset (`EOF`), and cloudflared's
own precheck reports both UDP and TCP connectivity as failed. `pinggy` over
ssh:443 works; `TUNNEL_PROVIDER=auto` falls through to it. Free pinggy sessions
expire hourly with a new hostname, so `Tunnel.watch()` reconnects and the
supervisor restarts the bot to re-install the button.

Readiness must come from the provider's "connection registered" line, not from
the hostname it prints first — cloudflared prints the URL seconds before the
tunnel serves, and using it yields HTTP 530.

## Tests

```bash
python -m unittest discover -s gmgn -p 'test_*.py'
```

## GitHub auth

Remote: `origin` → `https://github.com/aaaaaa790ufie-commits/copy-trade-engine-solana.git`
Auth: Windows Credential Manager (`wincred` git credential helper) — no token file needed.

## DB schema

Key tables: `wallet_watch`, `token_scores`, `paper_positions`, `paper_trades`,
`engine_events`, `paper_account`, `paper_cooldowns`.
