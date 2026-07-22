# Sentinel — Session Report (final)

> **Auto-generated.** Updated 2026-07-21 23:50 UTC.

## Run Parameters

| Field | Value |
|-------|-------|
| **Mode** | `DRY_RUN=true`, `LIVE=false` |
| **Pipeline** | v6 — global rate-limiter (50ms = ~20 req/s) |
| **Config** | `config.toml` |
| **Venues** | Pump.fun, PumpSwap, Raydium AMM v4, Raydium CPMM |
| **WS** | Helius (per-wallet mentions) + public (per-wallet + program-level) |
| **RPC HTTP** | Helius for decode; public for fallback |
| **Funding wallet** | 3VM3zGbrcL4cJiAZpbeKb5t2yXrWZe6Xv9Tt8QynpGvp |

## Pipeline v6 — Runtime & Decode Stats

Pipeline started at **07:57:38 UTC**, killed graceful at **~23:50 UTC** after the user's request. **~16 hours continuous uptime.**

| Metric | Value |
|--------|-------|
| **Decoded OK** | 1,778 |
| **Decoded None** | 818,146 |
| **Success rate** | 0.2% |
| **Runtime** | ~15.9 h |
| **Avg decode rate** | ~112 events/min |

The 818k "none" count is dominated by the public WS program-level subscription
(program log → spawn fetch_and_decode → getTransaction returns null because
txn too recent). Helius per-wallet mentions produce the 1,778 OK decodes.

## Trade History

**wallet_trades: 0 rows.** No trade ever passed the full pipeline (notification
→ rate-limited fetch → decode → filter → risk → executor → wallet_trades).

The filter correctly blocks non-tracked wallets (Tier C). The 1,778 decoded
events came from program-level subscriptions on the public WS — none matched
the 5 per-wallet subscribed addresses.

## Wallet Scores (from `wallet_scores` table)

| Tier | Scoring Status | Count | Avg Edge Score | Total PnL (SOL) |
|------|---------------|:----:|:--------------:|:----------------:|
| A | `no_data` | 4 | 1.0000 | 0.0000 |
| A | `ok` | 1 | 35.5465 | +1.7866 |
| C | `ok` | 12 | -0.8516 | -3.6056 |

**5 Tier-A wallets:**

1. **JDJW8HQPGBdFEQ5wMiDuQVvhtxq1i85BuJB9GT2d8WEG** — edge=+35.55, pnl=+1.79 SOL, 96 tx (16 win/80 loss), 112.8 tx/wk. Scoring_status: `ok`. Status: `tracked` in candidate_wallets.
2. **5tzFkiKscXHK5ZXCGbXZxwQBwwiDmP3p1WAMEREbmwBK** — no_data (seed wallet, never traded)
3. **DRpbwCxPqvNsKGMNchPkBLFxDSrGPzau7kRbnvjyYvK** — no_data
4. **F6UoN7AoUCcWMctBE26E1BQrYGEk8GnGPAhq8aY9X3eK** — no_data
5. **GjEtGzHafgEWsUF3WVqCjYLczHGB1hLrYjhPJ7CoynJp** — no_data

## Candidate Wallets

| Status | Count |
|--------|:----:|
| `tracked` | 1 (JDJW8HQPGBdF) |
| `dropped` | 12 |
| **Total** | **13** |

Discovered tokens: **21** (from discovery run).

## JDJW8HQPGBdF — Status

- **No new trades detected** during this session (~16 h pipeline runtime + previous runs).
- Pipeline subscribed per-wallet on both Helius and public WS.
- Zero log notifications received for this address. If the wallet made a trade,
  Helius `mentions` subscription would trigger → 50ms rate-limited getTransaction
  → decode → filter (match tracked wallet) → risk → executor (dry-run) → wallet_trades.
- Expected at 112.8 tx/wk ≈ 16 trades/day ≈ 0.67 tx/h. Over 16 h, P(0) ≈ 50%
  (Poisson). Statistically consistent with an inactive window.
- Edge score +35.55 is real but concentration risk flagged: 86% PnL from 2
  trades out of 96. The wallet is not consistently profitable.

## WARP Tunnel Infrastructure

| Component | Status |
|-----------|--------|
| `scripts/gen-warp-config.py` | ✅ Created — generates WARP config per-wallet |
| `scripts/check-helius.py` | ✅ Created — diagnostics for Helius upstream |
| `scripts/start-warp-helius.ps1` | ✅ Created — brings WARP tunnel up |
| WARP config (`~/Desktop/warp-youtube-only.conf`) | ✅ Saved — routing helius-rpc.com via WARP, youtube.com excluded |
| Helius API key | In config.toml, NOT behind WARP currently |

WARP tunnel was built to bypass intermittent Cloudflare SSL resets on 
Helius HTTP. Not activated — pipeline ran against Helius directly for this
session without issues after the rate-limiter was deployed.

## Bugs Fixed in This Session

1. **discovery/early_buyer.py**: Raydium AMM v4 program ID was 42-char (base58
   leading-zero truncation). Fixed to correct 44-char
   `675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8`.
2. **scorer/pnl_parser.py**: Same program ID bug. Also fixed `_token_deltas()`
   to scan ALL account indices instead of only the wallet's own index
   (token ATAs often have different accountIndex than the wallet).
3. **scorer/db.py**: `upsert_wallet_score` no longer overwrites valid `ok` or
   `no_data` scores with `rpc_failed`. Added `scoring_status` field.
4. **scorer/run_scorer.py**: RPC-failed wallets get tier=`N/A` and
   status=`retry_rpc` instead of being dropped. Added 150ms throttle between
   getTransaction calls. Added `error_count` tracking per wallet.
5. **src/ingest.rs**: String slice `[..8]` panicked on short mints/addresses
   (< 8 bytes). Changed to `.len().min(8)`.
6. **src/ingest.rs**: Added global shared rate-limiter (50ms spacing, ~20 req/s)
   via `OnceLock<Mutex<Instant>>` — all 4 concurrent workers serialised.
7. **dashboard/app.py**: Added `check_same_thread=False` for Streamlit+SQLite.

## Risks & Caveats

- Public RPC (`api.mainnet-beta.solana.com`) uses ZeroSSL certificate not
  trusted by reqwest+rustls-tls. HTTP calls through this endpoint fail silently
  (TLS handshake error → `None` despite WS being connected).
- Helius 25 req/s free tier is now respected (20 req/s with 50ms spacing).
- All scores truncated at 100 txns per wallet (Helius limit).
- Executor is `DRY_RUN=true, LIVE=false` — no real transactions sent.
