# Sentinel — Session Report

> **Auto-generated.** Updated periodically during unattended paper-trading run.
> Last refresh: see timestamps below.

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

## Wall-Clock Duration

_Will be filled after run._

## Trade Counts

_Will be filled after run, broken down by venue and pricing_method._

## Wallet Tiers

_Will be filled after run._

## PnL Comparison

_Will be filled after run: raw vs lagged+fee+slippage-adjusted._

## Errors / Rate-Limit Hits / Blocked Venues

_Will be filled after run._

## Still UNVERIFIED

The following items remain unverified or unresolved as of this session:

1. **Pump.fun bonding-curve PDA seeds** — `"bonding-curve"` is the documented seed,
   but Helius free-tier `getAccountInfo` returned `AccountNotFound` for every
   pumped token PDA tried. This may mean:
   - The seeds are different (need to extract from a real CPI or full-archive RPC)
   - The tokens checked had already graduated to Raydium (bonding curve closed)
   - Helius returns data via `getProgramAccounts` only (blocked on free tier)
   **Impact**: Pump.fun trades will be `pricing_method='naive'` until corrected.

2. **PumpSwap pool PDA seeds** — `["pool", base_mint, quote_mint]` is the documented
   pattern; not tested on-chain yet.

3. **Raydium API v3 fallback** — `api-v3.raydium.io` timeout/rate-limit behaviour
   during real trading hours not characterised.

4. **Position manager (TP/SL)** — tested in Phase 7 but in-memory only; SQLite
   persistence not implemented.

5. **Live `getSlot` behaviour** — the `wait_lag_duration()` uses a fixed sleep of
   `lag_slots × 400ms + 200ms`. If Solana slot times differ significantly from
   400ms, the actual lag will be off (but pricing still uses whatever pool state
   is current at the actual RPC call, so no more wrong than `naive`).

## Command to Restart

```bash
cd ~/sentinel && OPENSSL_DIR="C:\Users\Admin\openssl-mingw-extracted\mingw64" cargo run
```

## Known Code Warnings

28 compiler warnings (unused imports, dead fields, struct fields never read).
No errors. Test suite: 1 pass (PDA derivation), 1 pass (spot-check, structural).
