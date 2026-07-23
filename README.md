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

A token entry requires **weighted score >= 1.0**, equal to 4 wallets at 70%+, 16 wallets at 60-70%, or 32 wallets at 50-60%. Wallets must have BUY as their latest observed action for that token inside the 30-minute convergence window. Existing positions are never re-entered, and a closed token has a 7-minute cooldown.

This is a ranking heuristic, not a probability claim. Wallets can be correlated, copy one another, or all be wrong together.

## Paper account and exits

- Initial paper balance: `0.1 SOL`.
- Stake per entry: `0.025 SOL`, at most four full-size entries before the account is fully invested.
- Entries and open positions are priced with an independent `gmgn-cli token info` quote (feed trade prices are only a fallback), so stops keep firing even when a token drops out of the Smart Money feed.
- Trailing stop activates at `+25%` and trails the peak by `15%`.
- Emergency hard stop: `-45%`, selling the complete paper position.
- Max holding time: positions older than `GMGN_MAX_HOLD_SECONDS` (default 6h) are force-closed at the current mark price.
- Every entry and exit is timestamped in UTC seconds in SQLite and exposed through Telegram.
- Being fully invested is NOT bankruptcy. Only when the balance cannot cover the next stake **and no positions are open** is the account marked bankrupt, recording: `обнулились в papertrading, скажи это своему hermes agent, будем разбираться по сделкам`. If the balance later recovers to cover a stake, the flag resets and a `RECOVERY` event is journaled.

## Run

Configure the read-only GMGN key:

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

Both variables are required: the bot answers only its owner chat and refuses to start without `TELEGRAM_CHAT_ID`.

Telegram commands: `/status`, `/trades`, `/wallets`.

Environment overrides:

```text
GMGN_CHAINS=sol,robinhood
PAPER_BUDGET_SOL=0.1
PAPER_TRADE_SIZE_SOL=0.025
GMGN_ENTRY_SCORE=1.0
GMGN_CLUSTER_WINDOW_SECONDS=1800
GMGN_COOLDOWN_SECONDS=420
TRAILING_ACTIVATE_PCT=25
TRAILING_DISTANCE_PCT=15
HARD_STOP_PCT=45
GMGN_MAX_HOLD_SECONDS=21600
GMGN_ZERO_WINRATE_TTL_SECONDS=3600
GMGN_PRICE_TTL_SECONDS=60
GMGN_POLL_SECONDS=15
SENTINEL_DB=sentinel.db
SEED_WALLETS_SOL=data/seed_wallets_sol.txt
```

Wallet hygiene: only wallets with a **confirmed** sub-50% win rate are blacklisted. Wallets whose stats simply have not been fetched yet (win rate still 0) are dropped from the watch list after `GMGN_ZERO_WINRATE_TTL_SECONDS` without being blacklisted, so an API hiccup or rate limit can never permanently ban a good wallet. Manual seeds are never auto-dropped.

`gmgn-cli track smartmoney` does not require `GMGN_PRIVATE_KEY`; this project never calls GMGN swap endpoints. Robinhood support is attempted through the API and degrades to a logged warning if that route is unavailable.

The supplied 16 Solana wallets are stored in `data/seed_wallets_sol.txt` and are loaded into the watch journal as manual seeds at engine start (`run_engine.py`, source `manual_seed`; blacklisted addresses are skipped). The attached 80-address CSV contains `0x` EVM addresses, not Solana or Robinhood addresses, so it is not silently mixed into the Solana strategy. It needs a separate EVM adapter and is intentionally excluded for now.

## Safety

This branch is paper-only. The old Rust binary and its `dry_run=true`, `live=false` gates remain, but they are not needed by the new monitor. No private key, wallet signing, swap submission, or real SOL movement is present in the GMGN path. The Rust executor builds venue instructions for research purposes only — it contains no signer and never submits transactions.

Secrets are never hardcoded: the Helius health check (`scripts/check-helius.py`) reads `HELIUS_API_KEY` (or `SOLANA_API_KEY`) from the environment, and the GMGN key lives in `gmgn-cli config`. Do not commit `.env` files or keys.
