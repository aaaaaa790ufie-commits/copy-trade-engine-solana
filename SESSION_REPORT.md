# Sentinel — Session Report

> **Auto-generated.** Updated 2026-07-21 08:16 UTC.

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
| **WS subscription** | ✅ Per-wallet `mentions` (Helius: 4 wallets; Public: 4 wallets) |

## Decode Stats (live counter from this run)

| Metric | Value |
|--------|-------|
| **Decoded OK** | 19 |
| **Decoded None** | 8756 |
| **Success rate** | 0.2% |
| **Uptime** | ~7 min |
| **Avg rate** | ~21 events/min |

"None" cases are 429 rate-limited `getTransaction` calls. Helius HTTP
hit its free-tier 25 req/s cap — shared between the Rust pipeline,
Python discovery (20 tokens), and Python scorer (13 wallets × up to
100 txns each).

## Trade Counts

**0 trades logged.** `wallet_trades` table not created.

All 19 decoded events came from non-tracked wallets (Gygj9QQb,
AK2HKRnL, 8uXNFoqQ, 6AmzvTc5, etc.). The filter correctly classifies
them as Tier C — not forwarded to executor.

## Wallet Tiers (updated by scorer)

| Tier | Wallets | Source |
|------|---------|--------|
| A | 4 | `discovery/seed_wallets.txt` (hardcoded seed) |
| B | 0 | — |
| C | 7 | Scored by `run_scorer.py` (all edge=-1.0 — no trade data b/c 429) |
| Pending | 6 | Not scored (429 on getSignaturesForAddress) |

Scorer updated candidate_wallets: 7 dropped (C), 6 still pending.

## Discovery Results (this run via Helius HTTP)

| Metric | Value |
|--------|-------|
| **Tokens discovered** | 21 |
| **Candidate wallets found** | 13 (+12 new) |
| **Cross-ref wallets (≥2 tokens)** | 2 |
| **Seed wallets loaded** | 0 |
| **429 rate-limit hits** | Heavy (started on token 5/20, got worse) |

### Bugs fixed during this session

- `discovery/early_buyer.py`: Raydium AMM v4 program ID was 42-char
  invalid (base58 leading-zero truncation). Fixed to correct 43-char
  `675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8`.
- `scorer/pnl_parser.py`: Same Raydium AMM v4 program ID bug fixed.

## Errors / Rate-Limit Hits / Blocked Venues

- Helius HTTP severely rate-limited (429) on `getTransaction` and
  `getSignaturesForAddress` — 25 req/s free tier saturated by 3
  concurrent consumers (pipeline + discovery + scorer).
- Helius WS closed once at 05:14:49 UTC (`Away` — inactivity timeout).
  Reconnect observed (pipeline continued printing stats).
- Public RPC (`api.mainnet-beta.solana.com`) also 429s on Python scripts.
- All 4 venues producing events (PumpFun, PumpSwap visible in decoded log).
- No fatal errors; pipeline stayed up.

## Still UNVERIFIED

1. **Pump.fun bonding-curve PDA seeds** — `"bonding-curve"` seed not confirmed.
2. **Raydium API v3 fallback** — not tested under load.
3. **Position manager TP/SL** — in-memory only; SQLite persistence not implemented.
4. **Live slot-based lag** — `wait_lag_duration()` uses fixed 400ms/slot estimate.
5. **Executor sendTransaction** — blocked on DRY_RUN + LIVE=false.

## Command to Restart

```bash
cd ~/sentinel && OPENSSL_DIR="C:\Users\Admin\openssl-mingw-extracted\mingw64" cargo run
```
