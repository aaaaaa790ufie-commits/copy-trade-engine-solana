# Sentinel: weighted GMGN paper trader

The runtime is now intentionally small: GMGN Smart Money activity -> wallet
stats -> weighted convergence -> paper account -> Telegram journal. The old
raw-RPC discovery/decoder remains in the repository as legacy code, but is not
used by the paper runtime.

## Strategy

Only BUY events from wallets active within the last 7 days are considered.
Wallets with GMGN 30d win rate below 50% are ignored. Signal weight is:

| win rate | weight |
|---|---:|
| 70%+ | 0.25 |
| 60% to 70% | 0.0625 |
| 50% to 60% | 0.03125 |

A token enters paper trading when the distinct-wallet weight reaches **1.0**
inside a 30-minute window. This is a heuristic score, not a probability claim:
wallets may be correlated or follow the same source.

An open token cannot be entered again. After exit it has a **10-minute cooldown**
(default, configurable with `TOKEN_COOLDOWN_SECONDS`). Exit rules are deliberately
conservative: trailing activates at **+25%**, trails 15% below the peak, and an
emergency stop closes 100% at **-45%**.

## Paper account

Starting balance is **0.1 SOL** and every entry reserves **0.025 SOL**. If the
balance is below 0.025 SOL, the runtime stops and prints the required zeroed-out
message. Every entry/exit is stored in SQLite with UTC time to seconds, token,
price, strength, wallet count, PnL and exit reason.

## Run

```bash
pip install -r paper/requirements.txt
npm install -g gmgn-cli
gmgn-cli config --check
python -m paper.runtime
```

Set `TELEGRAM_BOT_TOKEN` and optionally `TELEGRAM_CHAT_ID`, then run the bot in a
second process:

```bash
python -m paper.telegram_bot
```

Bot commands: `/status`, `/trades`, `/wallets`, `/help`. The bot is read-only
with respect to trading: it only reports SQLite state and never calls GMGN swap.
`gmgn-cli` must be configured with an API key; no private key is needed.

## Seeds

The supplied Solana list is stored in `data/seed_solana_wallets.txt`. The
attached CSV contains EVM-style `0x` addresses and must be treated as the
separate Robinhood seed source, not mixed into Solana. Import it only after
confirming GMGN's Robinhood feed exposes the same stats fields. `track smartmoney`
is documented for Solana/BSC/Base/Ethereum, not Robinhood, so the current paper
collector is Solana-only and fails closed instead of pretending Robinhood data
is available.

## Safety

No real trade path is used by this runtime. The existing Rust configuration
still defaults to `dry_run=true` and `live=false`; do not fund or enable live
execution while paper results are being collected.
