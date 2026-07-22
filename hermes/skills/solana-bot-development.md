# Solana Bot Development

Patterns and practices for building Solana trading bots — free-tier RPC strategy, PnL parsing from raw transactions, WS subscription patterns, rate limiting, and project workflow conventions.

Based on work with the Sentinel copy-trade engine (Solana memecoin tracking).

## Free-Tier RPC Strategy

### Verified working providers

| Provider | Type | WS | HTTP | Key needed? | Notes |
|----------|------|---|---|-------------|-------|
| `mainnet.helius-rpc.com/?api-key=...` | Helius | ✓ | ✓ | Yes (UUID) | Primary free tier 25 req/s |
| `api.mainnet-beta.solana.com` | Public Solana | ✓ | ✓ | No | ZeroSSL cert — needs native-tls |

### Per-wallet mentions subscription

Subscribe per tracked wallet instead of per program:

```rust
{ "mentions": ["5tzFkiKscXHK5ZXCGbXZxwQBwwiDmP3p1WAMEREbmwBK"] }
```

`logsSubscribe` with `mentions` matches **any account key** in the transaction — not just program IDs. Reduces WS message volume by ~99.9% when wallets aren't actively trading.

### Warning: concurrent RPC consumers

Helius free tier is 25 req/s. Running Rust pipeline + Python discovery + scorer simultaneously saturates this in seconds. **Run in sequence.**

## Global Rate Limiter (Production-Grade)

Use a shared virtual clock across all workers:

```rust
use std::sync::{Mutex, OnceLock};

const MIN_RPC_INTERVAL: Duration = Duration::from_millis(50); // 20 req/s

fn next_rpc_slot() -> Duration {
    static LAST: OnceLock<Mutex<tokio::time::Instant>> = OnceLock::new();
    let last = LAST.get_or_init(|| Mutex::new(tokio::time::Instant::now()));
    let mut guard = last.lock().unwrap();
    let now = tokio::time::Instant::now();
    let earliest = std::cmp::max(*guard, now);
    let sleep = if earliest > now { earliest - now } else { Duration::ZERO };
    *guard = earliest + MIN_RPC_INTERVAL;
    sleep
}
```

### Interval guide

| Interval | Max req/s | Use case |
|----------|-----------|----------|
| 200ms | 5 | Conservative — public RPC + Helius |
| 100ms | 10 | Moderate — single provider |
| 50ms | 20 | Aggressive — Helius free tier with headroom |

### Simpler alternative: per-worker + jitter

```rust
let nanos = std::time::SystemTime::now()
    .duration_since(std::time::UNIX_EPOCH)
    .unwrap_or_default()
    .as_nanos();
let jitter_ms = (nanos % 51) as u64;
tokio::time::sleep(std::time::Duration::from_millis(250 + jitter_ms)).await;
```

## Warning: Per-wallet notification venue filter

`mentions: [wallet]` subscription DELIVERS notifications. But `parse_logs_direction()` checks logs for known DEX programs. If the wallet uses a **non-tracked venue** (e.g. OKX DEX v3, Jupiter), the notification is silently dropped — no counter incremented.

**Diagnosis:** check actual wallet activity via `getSignaturesForAddress` → parse `blockTime`. If recent activity exists but pipeline shows 0 decodes, the venue gate drops them.

## PnL Parsing from Raw Transactions

### Pre/post token balances

Solana `getTransaction` with `"jsonParsed"` returns `preTokenBalances` and `postTokenBalances` arrays. Parse all account indices (not just the wallet's own index — ATA is at a different index):

```python
# Correct: aggregate across ALL account indices
for entry in pre_tokens:
    mint = entry.get("mint", "")
    amount = entry.get("uiTokenAmount", {}).get("uiAmount")
    if amount is not None and mint:
        pre_by_mint[mint] = pre_by_mint.get(mint, 0.0) + amount
```

### Base58 leading-zero-byte pitfall

Raydium AMM v4 program ID is `675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8` (43 chars, 32 bytes). A 42-char variant decodes to only 31 bytes → RPC rejects with `WrongSize`. Verify: `assert len(b58decode(addr)) == 32`.

### Rust: byte-slicing panic on short mints

`&event.token_mint[..8]` panics if string < 8 bytes. Fix:
```rust
&event.source_wallet[..event.source_wallet.len().min(8)]
```

### Scoring status: protect against rpc_failed overwrite

`INSERT OR REPLACE` destroys a valid `ok` score when a later run returns `rpc_failed`. Check existing row before upsert:

```python
if old and old[0] in ('ok', 'no_data') and scoring_status == 'rpc_failed':
    # Skip replace, just update timestamp
    conn.execute("UPDATE wallet_scores SET last_scored_at = datetime('now') WHERE wallet_address = ?", (addr,))
    return
```

### Edge score concentration check

Before promoting a Tier A candidate, analyze risk:

```python
# Top-3 wins >90% of profit + <5 round-trips → lucky streak, not skill
```

### Pump.fun instruction encoding

| Direction | Discriminator | Data |
|-----------|--------------|------|
| Buy | `SHA256("global:buy")[:8]` | amount + max_sol_cost |
| Sell | `SHA256("global:sell")[:8]` | amount + min_sol_return |

### Raydium AMM v4 swap

Instruction index: `9`
Data: `0x09 + amount_in(8) + min_amount_out(8)`
~18 accounts

## Streamlit Dashboard

### First-run email prompt

```bash
echo "" | streamlit run dashboard/app.py --server.port=8501
```

### SQLite thread-safety

```python
sqlite3.connect(str(DB_PATH), check_same_thread=False)
```
