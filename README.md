# Sentinel — Solana Smart-Money Copy-Trading Engine

A self-hosted engine that discovers smart wallets from public on-chain data,
tracks their trades in near-real-time using only **free-tier RPC**, scores them
on realized PnL distribution (not just win-rate), and selectively mirrors trades
with independent risk management.

## Architecture

| Layer | Language | Role |
|---|---|---|
| **Hot path** (ingest → filter → risk → executor → position manager) | Rust | Latency-critical; single binary, internal modules via tokio mpsc |
| **Discovery / Scoring / Telemetry** | Python | Periodic batch jobs, pandas/numpy for analytics |
| **Dashboard** | Python (Streamlit) | Read-only local web UI over SQLite |
| **Data feed** | — | Free-tier RPC pool, WebSocket-first (no paid gRPC) |
| **Persistence** | — | SQLite |

## Safety

- **Double-gate**: `DRY_RUN=true` + `LIVE=false` by default. A real
  `sendTransaction` requires **both** `DRY_RUN=false` AND `LIVE=true`.
- **Isolated wallet**: a fresh keypair is generated during setup. The agent never
  funds it. The codebase never touches the Fasol wallet or any other external
  wallet.
- **Position limits**: hard caps in `config.toml` (max concurrent positions, max
  % per position, max % per source wallet), enforced in code.
- **Security pre-checks**: LP lock, mint authority, freeze authority, holder
  concentration — checked before every buy.
- **`.gitignore`** blocks `.env*`, `*.key`, `wallets/`.

## Free-Tier Constraint

Every data source and RPC endpoint in this project is reachable on a free tier.
No paid infrastructure is required. WebSocket subscriptions (`logsSubscribe`)
over free-tier keys (Helius, Alchemy, QuickNode, GetBlock) provide push-based
data without burning request quota.

See `config.toml` for the RPC pool configuration and priority weighting.

## Quick Start

```bash
# 1. Copy and populate environment
cp .env.example .env
# Edit .env with your free-tier API keys (see .env.example for variable names)

# 2. Run discovery (Python)
cd discovery && pip install -r requirements.txt && python run_discovery.py

# 3. Build and run the engine (Rust)
cd .. && cargo build --release && ./target/release/sentinel
```

## Phases

See [PROGRESS.md](./PROGRESS.md) for current build status and [config.toml](./config.toml)
for all tunable parameters.
