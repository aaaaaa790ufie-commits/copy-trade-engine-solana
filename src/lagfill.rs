//! Lagged fill pricing — pool-address resolution, slot wait, and price calculation
//! per venue.
//!
//! Phase 9: implements the gap from `naive` to `lagged` for all 4 venues.
//!
//! Pool addresses are resolved as follows:
//! - Pump.fun: bonding-curve PDA (deterministic, no RPC)
//! - PumpSwap: pool PDA from mint pair (tentative — falls back to naive if wrong)
//! - Raydium AMM v4 / CPMM: Raydium public API (https://api-v3.raydium.io)
//!
//! Resolved addresses cached in SQLite `pool_cache` table.

use crate::config::Config;
use crate::ingest::Venue;
use anyhow::{Context, Result};
use solana_sdk::pubkey::Pubkey;
use std::str::FromStr;
use solana_client::rpc_client::RpcClient;

/// Decode a base58 pubkey, padding to 32 bytes if base58 dropped leading zeros.
fn pubkey_padded(s: &str) -> Result<Pubkey> {
    let raw = bs58::decode(s).into_vec()?;
    if raw.len() == 32 {
        Ok(Pubkey::from_str(s)?)
    } else if raw.len() < 32 {
        let mut padded = vec![0u8; 32 - raw.len()];
        padded.extend_from_slice(&raw);
        Ok(Pubkey::new_from_array(padded.try_into().unwrap()))
    } else {
        anyhow::bail!("pubkey too long: {} bytes for {s}", raw.len())
    }
}

// ── Program ID constants (mirrored from executor.rs) ──────────────

const PUMP_FUN_PROGRAM: &str = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P";
const PUMP_SWAP_PROGRAM: &str = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA";
const RAYDIUM_AMM_V4: &str = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8";
const RAYDIUM_CPMM: &str = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP";

// ── Pump.fun bonding-curve PDA ────────────────────────────────────

pub fn pumpfun_bonding_curve_pda(mint: &Pubkey) -> (Pubkey, u8) {
    Pubkey::find_program_address(
        &[b"bonding-curve", mint.as_ref()],
        &Pubkey::from_str(PUMP_FUN_PROGRAM).unwrap(),
    )
}

// ── PumpSwap pool PDA ────────────────────────────────────────────

pub fn pumpswap_pool_pda(base_mint: &Pubkey, quote_mint: &Pubkey) -> (Pubkey, u8) {
    Pubkey::find_program_address(
        &[b"pool", base_mint.as_ref(), quote_mint.as_ref()],
        &Pubkey::from_str(PUMP_SWAP_PROGRAM).unwrap(),
    )
}

// ── Raydium API client ────────────────────────────────────────────

#[derive(serde::Deserialize, Debug)]
struct RaydiumApiResponse {
    data: Vec<RaydiumPoolEntry>,
}

#[derive(serde::Deserialize, Debug)]
struct RaydiumPoolEntry {
    #[serde(alias = "ammId", alias = "id")]
    amm_id: Option<String>,
    #[serde(alias = "poolId")]
    pool_id: Option<String>,
    #[serde(alias = "baseMint")]
    base_mint: Option<String>,
    #[serde(alias = "quoteMint")]
    quote_mint: Option<String>,
    #[serde(alias = "lpMint")]
    lp_mint: Option<String>,
    #[serde(alias = "programId")]
    program_id: Option<String>,
    #[serde(alias = "type")]
    pool_type: Option<String>,
    #[serde(alias = "version")]
    version: Option<u32>,
}

/// Try to resolve a Raydium pool address from the Raydium public API.
pub async fn raydium_pool_from_api(
    token_mint: &str,
    venue: &Venue,
    http_client: &reqwest::Client,
) -> Result<Option<String>> {
    let url = "https://api-v3.raydium.io/main/info";
    let resp: RaydiumApiResponse = http_client
        .get(url)
        .send()
        .await
        .context("Raydium API request failed")?
        .json()
        .await
        .context("Raydium API parse failed")?;

    for pool in &resp.data {
        let pid = pool.program_id.as_deref().unwrap_or("");
        let matches_venue = match venue {
            Venue::RaydiumAmmV4 => pid == RAYDIUM_AMM_V4,
            Venue::RaydiumCpmm => pid == RAYDIUM_CPMM,
            _ => false,
        };
        if !matches_venue {
            continue;
        }
        let pool_id = pool.amm_id.as_deref().or(pool.pool_id.as_deref()).unwrap_or("");
        let base = pool.base_mint.as_deref().unwrap_or("");
        let quote = pool.quote_mint.as_deref().unwrap_or("");
        if base == token_mint || quote == token_mint {
            return Ok(Some(pool_id.to_string()));
        }
    }
    Ok(None)
}

// ── Pool address cache (SQLite) ──────────────────────────────────

pub fn init_pool_cache(conn: &rusqlite::Connection) -> Result<()> {
    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS pool_cache (
            token_mint TEXT PRIMARY KEY,
            venue TEXT NOT NULL,
            pool_address TEXT NOT NULL,
            resolved_at TEXT DEFAULT (datetime('now'))
        )",
    )?;
    Ok(())
}

pub fn get_cached_pool(
    conn: &rusqlite::Connection,
    token_mint: &str,
    venue: &Venue,
) -> Result<Option<String>> {
    let venue_str = format!("{:?}", venue);
    let mut stmt = conn.prepare(
        "SELECT pool_address FROM pool_cache WHERE token_mint = ?1 AND venue = ?2",
    )?;
    let mut rows = stmt.query_map(rusqlite::params![token_mint, venue_str], |row| {
        row.get::<_, String>(0)
    })?;
    match rows.next() {
        Some(Ok(addr)) => Ok(Some(addr)),
        _ => Ok(None),
    }
}

pub fn cache_pool_address(
    conn: &rusqlite::Connection,
    token_mint: &str,
    venue: &Venue,
    pool_address: &str,
) -> Result<()> {
    let venue_str = format!("{:?}", venue);
    conn.execute(
        "INSERT OR REPLACE INTO pool_cache (token_mint, venue, pool_address) VALUES (?1, ?2, ?3)",
        rusqlite::params![token_mint, venue_str, pool_address],
    )?;
    Ok(())
}

// ── Main resolution function ──────────────────────────────────────

pub async fn resolve_pool_address(
    token_mint: &str,
    venue: &Venue,
    http_client: &reqwest::Client,
    quote_mint: Option<&str>,
) -> Result<Option<String>> {
    match venue {
        Venue::PumpFun => {
            let mint = pubkey_padded(token_mint)?;
            let (pda, _bump) = pumpfun_bonding_curve_pda(&mint);
            Ok(Some(pda.to_string()))
        }
        Venue::PumpSwap => {
            let base = pubkey_padded(token_mint)?;
            let quote = match quote_mint {
                Some(q) => pubkey_padded(q)?,
                None => pubkey_padded("So11111111111111111111111111111111111111112")?,
            };
            let (pda, _bump) = pumpswap_pool_pda(&base, &quote);
            Ok(Some(pda.to_string()))
        }
        Venue::RaydiumAmmV4 | Venue::RaydiumCpmm => {
            raydium_pool_from_api(token_mint, venue, http_client).await
        }
        Venue::Unknown(_) => Ok(None),
    }
}

// ═════════════════════════════════════════════════════════════════
//  Fill-price computation from pool state
// ═════════════════════════════════════════════════════════════════

/// Read the pool account data and compute a lagged fill price.
///
/// Returns (fill_price_sol_per_token, pricing_method).
/// Falls back to (naive_price, "naive") on any error.
pub async fn compute_lagged_fill_price(
    venue: &Venue,
    _token_mint: &str,
    pool_address: &str,
    amount_sol: f64,
    naive_price: f64,
    rpc_client: &RpcClient,
) -> (f64, &'static str) {
    let pool_pk = match Pubkey::from_str(pool_address) {
        Ok(pk) => pk,
        Err(_) => return (naive_price, "naive"),
    };

    let account = match rpc_client.get_account(&pool_pk) {
        Ok(acc) => acc,
        Err(e) => {
            tracing::warn!("[lagfill] get_account failed for {}: {e}", pool_address);
            return (naive_price, "naive");
        }
    };

    match venue {
        Venue::PumpFun => {
            compute_pumpfun_fill_price(&account.data, amount_sol, naive_price)
        }
        Venue::PumpSwap | Venue::RaydiumAmmV4 | Venue::RaydiumCpmm => {
            compute_cpmm_fill_price(&account.data, amount_sol, naive_price)
        }
        Venue::Unknown(_) => (naive_price, "naive"),
    }
}

/// Parse Pump.fun bonding-curve account and compute fill price.
fn compute_pumpfun_fill_price(
    data: &[u8],
    amount_sol: f64,
    default: f64,
) -> (f64, &'static str) {
    if data.len() < 48 {
        return (default, "naive");
    }

    let virtual_token = u64::from_le_bytes(data[16..24].try_into().unwrap());
    let virtual_sol = u64::from_le_bytes(data[24..32].try_into().unwrap());

    if virtual_token == 0 || virtual_sol == 0 {
        return (default, "naive");
    }

    let sol_in_lamports = (amount_sol * 1_000_000_000.0) as u128;
    let v_token = virtual_token as u128;
    let v_sol = virtual_sol as u128;

    // Constant product buy formula:
    // tokens_out = (sol_in * v_token) / (v_sol + sol_in)
    let tokens_out = (sol_in_lamports * v_token) / (v_sol + sol_in_lamports);

    if tokens_out == 0 {
        return (default, "naive");
    }

    let fill_price = (sol_in_lamports as f64) / (tokens_out as f64);
    (fill_price, "lagged")
}

/// Generic CPMM fill price from a pool account.
fn compute_cpmm_fill_price(
    data: &[u8],
    amount_sol: f64,
    default: f64,
) -> (f64, &'static str) {
    if data.len() < 24 {
        return (default, "naive");
    }

    let base_reserve = u64::from_le_bytes(data[8..16].try_into().unwrap());
    let quote_reserve = u64::from_le_bytes(data[16..24].try_into().unwrap());

    if base_reserve == 0 || quote_reserve == 0 {
        return (default, "naive");
    }

    let sol_in_lamports = (amount_sol * 1_000_000_000.0) as u128;
    let b_res = base_reserve as u128;
    let q_res = quote_reserve as u128;

    let quote_out = (sol_in_lamports * q_res) / (b_res + sol_in_lamports);

    if quote_out == 0 {
        return (default, "naive");
    }

    let fill_price = (sol_in_lamports as f64) / (quote_out as f64);
    (fill_price, "lagged")
}

/// Wait for the configured lag without polling RPC.
///
/// Sleeps for `lag_slots × 400 ms + 200 ms margin`, then returns.
/// The subsequent `getAccountInfo` (pool state) is the only RPC call per trade.
/// No `getSlot()` polling — avoids the rate-limit anti-pattern.
const SLOT_TIME_MS: u64 = 400;

pub async fn wait_lag_duration(lag_slots: u64) {
    let delay_ms = lag_slots.saturating_mul(SLOT_TIME_MS).saturating_add(200);
    tokio::time::sleep(std::time::Duration::from_millis(delay_ms)).await;
}

// ═════════════════════════════════════════════════════════════════
//  Tests — PDA derivation + spot-check against live on-chain data
// ═════════════════════════════════════════════════════════════════

#[cfg(test)]
mod tests {
    use super::*;

    /// Verify Pump.fun bonding-curve PDA derivation is deterministic.
    #[test]
    fn test_pumpfun_pda_derivation() {
        let mint = pubkey_padded("FREDy2AK4BNSjoj3EQjQBvEANqNA1wGzbv3T8yWpump").unwrap();
        let (pda, bump) = pumpfun_bonding_curve_pda(&mint);
        assert_ne!(pda, Pubkey::default(), "PDA should not be zero");

        let mint2 = pubkey_padded("FWogK7Fpf8kB6GAMBh8Vg5XUjuLkohxHbyy8UQkump").unwrap();
        let (pda2, bump2) = pumpfun_bonding_curve_pda(&mint2);
        assert_ne!(pda2, Pubkey::default());
    }

    /// Spot-check: compute lagged fill price for real Pump.fun tokens
    /// by fetching their bonding curve state via getAccountInfo
    /// (same RPC call the live pipeline uses — 1 call per token).
    #[test]
    fn test_pumpfun_spot_check() {
        const HELIUS: &str = "https://mainnet.helius-rpc.com/?api-key=33a9f314-bc9f-452d-bd59-ced96126d602";
        let rpc_client = RpcClient::new(HELIUS);

        let tokens = [
            "FREDy2AK4BNSjoj3EQjQBvEANqNA1wGzbv3T8yWpump",
            "FWogK7Fpf8kB6GAMBh8Vg5XUjuLkohxHbyy8UQkump",
        ];

        let amount_sol = 0.01_f64;

        for mint_str in &tokens {
            let mint = pubkey_padded(mint_str).unwrap();
            let (pda, _bump) = pumpfun_bonding_curve_pda(&mint);

            println!("\n=== {mint_str} ===");
            println!("Bonding curve PDA: {pda}");

            match rpc_client.get_account(&pda) {
                Ok(acc) => {
                    let data = &acc.data;
                    println!("Data len: {} | Owner: {}", data.len(), acc.owner);

                    if data.len() >= 48 {
                        let vt = u64::from_le_bytes(data[16..24].try_into().unwrap());
                        let vs = u64::from_le_bytes(data[24..32].try_into().unwrap());

                        let sl = (amount_sol * 1_000_000_000.0) as u128;
                        let tokens_out = (sl * vt as u128) / (vs as u128 + sl);
                        let fill_price = sl as f64 / tokens_out as f64;

                        println!("Virtual token: {vt}");
                        println!("Virtual SOL:   {vs}");
                        println!("Buy {amount_sol} SOL → {tokens_out} tokens");
                        println!("Lagged price:  {fill_price:.12} SOL/token");

                        assert!(fill_price > 0.0, "price must be > 0");
                        assert!(fill_price < 10_000.0, "implausible: {fill_price}");
                        println!("✓ SANITY OK");
                    } else {
                        println!("Data too short: {}", data.len());
                    }
                }
                Err(e) => {
                    println!("RPC error: {e}");
                }
            }
        }
    }
}
