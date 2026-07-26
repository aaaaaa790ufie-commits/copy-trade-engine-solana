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
export TELEGRAM_BOT_TOKEN="<redacted-revoked-token>"
export TELEGRAM_CHAT_ID="6207459171"
python gmgn/telegram_bot.py
```

## Environment

`.env` is gitignored — contains Telegram token + chat ID.
Copy `.env.example` for template, or source .env manually:

```bash
export $(grep -v '^#' .env | xargs)
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
gmgn-cli  ──REST──►  gmgn/paper_engine.py  ──SQLite──►  gmgn/telegram_bot.py
  (read-only              │                                    │
   GMGN API)       weighted convergence         /status /trades /wallets
                    + paper account              + proactive push
                    + trailing/hard stops         (engine_events)
                    + wallet discovery
```

## Key parameters

| Env var | Default | Purpose |
|---|---|---|
| `GMGN_CHAINS` | `sol,robinhood` | Chains to poll (only `sol` works) |
| `GMGN_POLL_SECONDS` | 15 | Feed poll interval |
| `GMGN_ENTRY_SCORE` | 0.25 | Entry threshold (one 70%+ wallet = entry) |
| `PAPER_BUDGET_SOL` | 0.1 | Initial paper balance |
| `PAPER_TRADE_SIZE_SOL` | 0.025 | Stake per entry |
| `GMGN_CLUSTER_WINDOW_SECONDS` | 1800 | 30 min cluster window |
| `HARD_STOP_PCT` | 45 | Emergency exit level |

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
  `CreateProcess` on Windows; `_find_gmgn()` handles it.

## Telegram bot commands

| Command | Description |
|---|---|
| `/status` | Paper account balance + open positions |
| `/trades` | Last 10 trades |
| `/wallets` | Wallet pool stats |
| `/weights` | Top 10 token scores |
| `/help` | This menu |

## GitHub auth

Remote: `origin` → `https://github.com/aaaaaa790ufie-commits/copy-trade-engine-solana.git`
Auth: Windows Credential Manager (`wincred` git credential helper) — no token file needed.

## DB schema

Key tables: `wallet_watch`, `token_scores`, `paper_positions`, `paper_trades`,
`engine_events`, `paper_account`, `paper_cooldowns`.
