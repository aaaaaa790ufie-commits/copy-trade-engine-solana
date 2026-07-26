# Sentinel: GMGN weighted-convergence paper trading

The runtime path is intentionally small:

```text
GMGN Smart Money feed -> 7d activity + 30d win rate -> weighted convergence
-> paper wallet (0.1 SOL) -> Telegram-readable journal
```

## Strategy

A wallet is eligible when it traded during the last 7 days and its 30-day win rate is at least 50%. Signal weights:

| 30d win rate | Weight |
|---|---:|
| 50% to <60% | 0.03125 |
| 60% to <70% | 0.0625 |
| 70%+ | 0.25 |

A token entry requires the weighted score to reach `GMGN_ENTRY_SCORE` (default 0.25, i.e. one 70%+ wallet) inside a 30-minute window. Existing positions are never re-entered; a closed token has a cooldown. This is a ranking heuristic, not a probability claim.

Weights are read from the win rates already cached in `wallet_watch`, so the poll loop makes one feed call rather than twenty stats calls. Wallet bookkeeping (refresh, discovery, cleanup) runs on its own `GMGN_MAINTENANCE_SECONDS` timer, after the stop-loss check — never before it.

## Mass wallet discovery

`wallets-quality.txt` is a generated snapshot, not a hand-maintained truth source. The current committed snapshot is only a small sample. To rebuild it from current GMGN data and target up to 3,000 verified Solana wallets:

```bash
# check the API key first, then run from repository root
gmgn-cli config --check
python gmgn/mass_discovery.py --target 3000 --max-tokens 300 --min-winrate 0.50 --min-7d-trades 1 --min-30d-trades 5
```

The collector combines `track smartmoney`, `track kol`, trending and trench tokens, and `token traders`, then verifies candidates through `portfolio stats`. It writes atomically and applies the 7-day activity gate, 30-day win-rate gate, and minimum 30-day sample gate. It never calls swap and never needs `GMGN_PRIVATE_KEY`.

For a cheap preview:

```bash
python gmgn/mass_discovery.py --dry-run --target 3000 --max-tokens 30
```

Do not commit a generated multi-thousand-wallet snapshot blindly. Run it with the connected GMGN account, inspect the count and top rows, then commit the resulting `wallets-quality.txt`. The bot can keep running during discovery because the file replacement is atomic.

## Paper account and exits

- Initial paper balance: 0.1 SOL.
- Stake per entry: 0.025 SOL.
- Trailing stop: +25% activation, 15% trail.
- Emergency hard stop: -45%, full-position exit.
- Max holding time defaults to 6h.
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

Telegram commands: `/status`, `/positions`, `/trades`, `/wallets`, `/weights`, `/config`.

## Telegram Mini App

`python gmgn/webapp.py` serves a panel at <http://127.0.0.1:8770> with equity curve, live position P&L, trade history, the wallet pool and tokens approaching the entry threshold. It works in a plain browser as-is.

Telegram only opens Mini Apps over HTTPS, so expose it through a tunnel:

```bash
cloudflared tunnel --url http://127.0.0.1:8770
```

Put the resulting `https://…` URL into `WEBAPP_PUBLIC_URL` in `.env` and restart the bot; it installs the panel as the chat's menu button and as a keyboard button. Setting `WEBAPP_PUBLIC_URL` also turns on request signing: the API then requires valid Telegram `initData`, checked by HMAC against the bot token and pinned to `TELEGRAM_CHAT_ID`, so nobody else can read the panel through the tunnel.

The API is read-only (`/api/overview`, `/api/trades`, `/api/wallets`, `/api/weights`, `/api/events`, `/api/equity`) and opens the database in SQLite read-only mode.

## Tests

```bash
python -m unittest discover -s gmgn -p 'test_*.py'
```

The supplied 16 Solana wallets are in `data/seed_wallets_sol.txt`. The separate EVM CSV is not mixed into Solana data. The runtime is paper-only: no private key, signing, swap submission, or SOL movement.
