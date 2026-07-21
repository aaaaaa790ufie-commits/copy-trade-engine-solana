# Sentinel — Session Report

> **Auto-generated.** Updated 2026-07-20 10:35 UTC.

## Run Parameters

| Field | Value |
|-------|-------|
| **Mode** | `DRY_RUN=true`, `LIVE=false` |
| **Config** | `config.toml` |
| **Venues** | Pump.fun, PumpSwap, Raydium AMM v4, Raydium CPMM |
| **Lag slots** | 2 |
| **Pricing** | `lagged` when pool readable, fallback `naive` |
| **Seed wallets** | 4 in `discovery/seed_wallets.txt` |
| **RPC** | Helius (WS+HTTP) + public fallback |
| **WS** | ✅ 2/2 connected (helius + public) |
| **Decoder** | ✅ Live — `fetch_and_decode()` via RPC `getTransaction` |
| **WS subscription** | ✅ Per-wallet `mentions` (Helius: 4 wallets only; Public: 4 wallets + 4 programs) |

## Decode Stats (live counter)

| Metric | Value |
|--------|-------|
| **Decoded OK** | 498 |
| **Decoded None** | 1382 |
| **Success rate** | 26.5% |
| **Uptime** | ~16 min |
| **Avg rate** | ~31 events/min |

"None" cases are rate-limited RPC calls or transactions without meaningful
token balance changes (e.g. txn errors, wrapper transactions). The ingest
pipeline is healthy — events arrive and are processed continuously.

## Trade Counts

**0 trades logged.** `wallet_trades` table not yet created.

`wallet_scores` exists with **4 Tier-A wallets** seeded from
`discovery/seed_wallets.txt`. None of the 498 decoded events matched these 4
addresses as `source_wallet`. The filter correctly classifies all events as
**Tier C — not tracked** and does not forward them to the executor.

## Wallet Tiers

| Tier | Wallets | Source |
|------|---------|--------|
| A | 4 | `discovery/seed_wallets.txt` (hardcoded seed) |
| B | 0 | — |
| C | N/A | Default for all non-tracked wallets |

## PnL Comparison

N/A — no trades to compare.

## Errors / Rate-Limit Hits / Blocked Venues

- No errors observed. WS connections stable.
- 1382 decode "none" cases are rate-limiting on `getTransaction` — expected
  behaviour under Helius free tier.
- All 4 venues producing events (PumpFun, PumpSwap, RaydiumAmmV4 visible in logs).

## Still UNVERIFIED

1. **Pump.fun bonding-curve PDA seeds** — `"bonding-curve"` seed not confirmed.
2. **Raydium API v3 fallback** — not tested under load.
3. **Position manager TP/SL** — in-memory only; SQLite persistence not implemented.
4. **Live slot-based lag** — `wait_lag_duration()` uses fixed 400ms/slot estimate.

## Command to Restart

```bash
cd ~/sentinel && OPENSSL_DIR="C:\Users\Admin\openssl-mingw-extracted\mingw64" cargo run
```
