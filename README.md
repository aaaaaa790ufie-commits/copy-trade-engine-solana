# Sentinel: GMGN Smart Money Cluster Trader

Sentinel now has one job: find **converging Smart Money buys**, pass qualified
signals into the existing Rust risk/execution path, and paper-trade them safely.

## Simplified architecture

```text
GMGN Smart Money feed
  -> GMGN 30d wallet stats
  -> keep wallets with win rate >= 70%, >= 10 buys, positive realized PnL
  -> require >= 3 distinct qualified wallets whose latest action is BUY
     on the same token inside 30 minutes
  -> SQLite signal queue
  -> Rust filter -> risk -> executor (DRY_RUN)
```

The old `discovery/` and `scorer/` code remains for reference, but is no longer
on the runtime path. Raw Solana `logsSubscribe` decoding is also removed from
the runtime path. GMGN handles wallet discovery, venue coverage, activity, and
wallet statistics; Sentinel owns the strategy, risk rules, and execution.

## Important statistical note

Three wallets with a 70% historical win rate do **not** automatically imply a
97.3% success probability. Their decisions can be correlated, they may copy the
same source, and historical win rate is not calibrated probability. Sentinel
uses the three-wallet cluster as a strong ranking signal, not a probability
claim. Paper results must prove the edge before live trading is considered.

## Setup

```bash
npm install -g gmgn-cli
gmgn-cli config
python gmgn/monitor.py --self-test
cargo test
```

The read-only monitor needs a GMGN API key. It does **not** need
`GMGN_PRIVATE_KEY`, does not use GMGN swap endpoints, and cannot submit a trade.
GMGN OpenAPI is currently open to users; its documented limiter is a leaky
bucket, so the monitor polls every 15 seconds and caches wallet stats for 15
minutes.

## Run

Open two terminals from the repository root:

```bash
# Terminal 1: produce qualified cluster signals
python gmgn/monitor.py

# Terminal 2: consume signals and paper-trade
cargo run --release
```

Defaults are configurable with environment variables:

```text
GMGN_MIN_CLUSTER_WALLETS=3
GMGN_MIN_WINRATE=0.70
GMGN_MIN_BUYS_30D=10
GMGN_MIN_REALIZED_PROFIT_USD=0
GMGN_CLUSTER_WINDOW_SECONDS=1800
GMGN_POLL_SECONDS=15
GMGN_STATS_TTL_SECONDS=900
GMGN_FEED_LIMIT=200
SENTINEL_DB=sentinel.db
```

## Safety

`config.toml` still defaults to `dry_run=true` and `live=false`. A real
transaction requires both gates to be deliberately reversed. Keep them as-is
until the new GMGN pipeline has accumulated enough non-zero paper trades to
measure win rate after lag, fees, and slippage.

## What is intentionally not solved yet

GMGN reports token price in USD while the Rust paper-fill model expects SOL per
token. The monitor does not mislabel USD as SOL. It sends a zero reference price
and lets the executor resolve the pool and calculate an on-chain fill; unresolved
pools remain unsuitable for PnL analysis and should be treated as telemetry,
not evidence of profitability.
