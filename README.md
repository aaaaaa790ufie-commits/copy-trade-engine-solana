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

A token entry requires weighted score >= 1.0 inside a 30-minute window. Existing positions are never re-entered; a closed token has a cooldown. This is a ranking heuristic, not a probability claim.

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

## Run

```bash
npm install -g gmgn-cli
gmgn-cli config
python gmgn/run_engine.py
```

In a second terminal:

```bash
export TELEGRAM_BOT_TOKEN='...'
export TELEGRAM_CHAT_ID='...'
python gmgn/telegram_bot.py
```

Telegram commands: `/status`, `/trades`, `/wallets`.

The supplied 16 Solana wallets are in `data/seed_wallets_sol.txt`. The separate EVM CSV is not mixed into Solana data. The runtime is paper-only: no private key, signing, swap submission, or SOL movement.
