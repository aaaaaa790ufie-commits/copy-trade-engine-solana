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
# GMGN_CHAINS="sol" GMGN_ENTRY_SCORE="0.25" python gmgn/run_engine.py

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
tunables there rather than calling `os.getenv` at a use site.

## Cycle ordering (do not reorder)

`cycle()` runs: feed → **exits** → entries → throttled maintenance. Stop-loss checks
must stay ahead of anything that calls the stats API. They used to run after a
`get_stats()` sweep of every maker in the feed (up to 20 round-trips × 45 s timeout),
which delayed stops by tens of minutes and produced two −99.99% exits on a −45% hard
stop. `test_exits_run_before_wallet_stats` guards this.

Entry weights come from `cached_weights()` — the win rates already in `wallet_watch` —
so the fast path costs one API call. `refresh_wallet_stats` keeps those values current
in the background, bounded by `GMGN_STATS_BATCH_MAX`.

## Key parameters

| Env var | Default | Purpose |
|---|---|---|
| `GMGN_CHAINS` | `sol` | Chains to poll (only `sol` works) |
| `GMGN_POLL_SECONDS` | 15 | Feed poll interval |
| `GMGN_ENTRY_SCORE` | 0.25 | Entry threshold (one 70%+ wallet = entry) |
| `PAPER_BUDGET_SOL` | 0.1 | Initial paper balance |
| `PAPER_TRADE_SIZE_SOL` | 0.025 | Stake per entry |
| `GMGN_CLUSTER_WINDOW_SECONDS` | 1800 | 30 min cluster window |
| `HARD_STOP_PCT` | 45 | Emergency exit level |
| `GMGN_MAINTENANCE_SECONDS` | 600 | Wallet bookkeeping interval |
| `GMGN_STATS_BATCH_MAX` | 6 | Cap on stats round-trips per pass |
| `WEBAPP_PORT` | 8770 | Mini App server port |
| `WEBAPP_PUBLIC_URL` | — | HTTPS origin; enables the Telegram button **and** auth |

## Weight computation

```python
def weight(winrate: float) -> float:
    if winrate >= 0.70:  return 0.25    # ← single wallet can trigger entry
    if winrate >= 0.60:  return 0.0625
    if winrate >= 0.50:  return 0.03125
    return 0.0
```

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
