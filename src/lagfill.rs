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
use solana_sdk::pubkey::PUBKEY_BYTES;
use std::str::FromStr;
use solana_client::rpc_client::RpcClient;

// ── Program ID constants (mirrored from executor.rs) ──────────────

const PUMP_FUN_PROGRAM: &str = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P";
const PUMP_SWAP_PROGRAM: &str = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA";
const RAYDIUM_AMM_V4: &str = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8";
const RAYDIUM_CPMM: &str = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP";

// ── Pump.fun bonding-curve PDA ────────────────────────────────────
//
// The bonding-curve account is a PDA seeded with:
//     seeds = ["bonding-curve", mint.as_ref()]
//     program_id = Pump.fun (6EF8rrect...)
//
// This is deterministic and can be computed locally with no RPC call.

pub fn pumpfun_bonding_curve_pda(mint: &Pubkey) -> (Pubkey, u8) {
    Pubkey::find_program_address(
        &[b"bonding-curve", mint.as_ref()],
        &Pubkey::from_str(PUMP_FUN_PROGRAM).unwrap(),
    )
}

// ── PumpSwap pool PDA ────────────────────────────────────────────
//
// PumpSwap uses Anchor; the pool PDA is derived from the two token
// mints.  Exact seeds depend on the `init_pool` instruction.  Based on
// the Anchor IDL's `pool` account constraints, the common pattern is:
//     seeds = ["pool", base_mint.as_ref(), quote_mint.as_ref()]
//     program_id = PumpSwap (pAMMBay6...)
//
// This is tentative — if `find_program_address` doesn't match what's
// actually on-chain for a given pair, fall back to naive pricing for
// that token.

pub fn pumpswap_pool_pda(base_mint: &Pubkey, quote_mint: &Pubkey) -> (Pubkey, u8) {
    Pubkey::find_program_address(
        &[b"pool", base_mint.as_ref(), quote_mint.as_ref()],
        &Pubkey::from_str(PUMP_SWAP_PROGRAM).unwrap(),
    )
}

// ── Raydium API client ────────────────────────────────────────────
//
// Uses Raydium's public API to look up pool addresses by mint pair.
// The endpoint: https://api-v3.raydium.io/main/info
// This is preferred over getProgramAccounts (expensive on free RPC).

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

/// Try to resolve a Raydium pool address (AMM v4 or CPMM) from the
/// Raydium public API.
///
/// Returns the first matching pool's ID for the given venue + token_mint.
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

        let pool_id = pool
            .amm_id
            .as_deref()
            .or(pool.pool_id.as_deref())
            .unwrap_or("");

        let base = pool.base_mint.as_deref().unwrap_or("");
        let quote = pool.quote_mint.as_deref().unwrap_or("");

        if base == token_mint || quote == token_mint {
            return Ok(Some(pool_id.to_string()));
        }
    }

    Ok(None)
}

// ── Pool address cache (SQLite) ──────────────────────────────────
//
// Caches mint → pool_address so we don't hit the API or compute PDA on
// every trade.

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
//
// Tries PDA first (Pump.fun, PumpSwap), then API (Raydium), then
// returns None which the caller treats as "fall back to naive".

pub async fn resolve_pool_address(
    token_mint: &str,
    venue: &Venue,
    http_client: &reqwest::Client,
    quote_mint: Option<&str>,
) -> Result<Option<String>> {
    match venue {
        Venue::PumpFun => {
            let mint = Pubkey::from_str(token_mint)?;
            let (pda, _bump) = pumpfun_bonding_curve_pda(&mint);
            Ok(Some(pda.to_string()))
        }
        Venue::PumpSwap => {
            let base = Pubkey::from_str(token_mint)?;
            let quote = match quote_mint {
                Some(q) => Pubkey::from_str(q)?,
                None => {
                    Pubkey::from_str("So11111111111111111111111111111111111111112")?
                }
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
//
// After resolving a pool address and waiting the configured lag,
// read the pool account's current reserves and compute a fill price
// using the venue's market-making formula.
//
//# Pump.fun bonding curve (Account data layout)
//
// All numbers are u64 little-endian.  Offsets are from the start of
// the account's data (the Anchor/BPF discriminator is 8 bytes).
//
// | offset | field                  |
// |--------|------------------------|
// |   0    | discriminator (8 bytes) |
// |   8    | token_total_supply     |
// |  16    | virtual_token_reserves |
// |  24    | virtual_sol_reserves   |
// |  32    | real_token_reserves    |
// |  40    | real_sol_reserves      |
// |  48    | token_total_supply_2   |
// |  56    | complete (bool, 1 byte)|
//
// Buy formula (constant product against virtual reserves):
//   k = virtual_token_reserves * virtual_sol_reserves
//   tokens_out = (sol_in * virtual_token_reserves)
//              / (virtual_sol_reserves + sol_in)
//   price_sol_per_token = sol_in / tokens_out
//
// Called with the **lagged** pool state (N slots after the signal).
//
//# Raydium / PumpSwap CPMM (pool state — tentative)
//
// All CPMM-style pools expose two vault token accounts.  Their
// balances are read via getTokenAccountBalance and the fill price is:
//   price = (amount_in * quote_reserve) / (base_reserve + amount_in)
//   adjusted for the venue's swap fee.
//
// For now we parse from the pool-account bytes directly, assuming the
// simplest layout: base_reserve (u64) and quote_reserve (u64) at
// offsets 8 and 16.

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
    if data.len() < 64 {
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
///
/// Assumes the simplest layout: 8-byte discriminator, then
/// base_reserve (u64) and quote_reserve (u64).
/// Overridden per-venue when real byte-offsets are known.
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

    // CPMM: quote_out = (sol_in * q_res) / (b_res + sol_in)
    let quote_out = (sol_in_lamports * q_res) / (b_res + sol_in_lamports);

    if quote_out == 0 {
        return (default, "naive");
    }

    let fill_price = (sol_in_lamports as f64) / (quote_out as f64);
    (fill_price, "lagged")
}

/// Get the current slot from RPC.
pub fn get_current_slot(rpc_client: &RpcClient) -> Result<u64> {
    Ok(rpc_client.get_slot()?)
}

/// Wait (polling) until the current slot >= target_slot.
/// Returns an error on timeout.
pub async fn wait_for_slot(
    rpc_client: &RpcClient,
    target_slot: u64,
    max_poll_ms: u64,
) -> Result<()> {
    let poll_interval = std::time::Duration::from_millis(400);
    let deadline = std::time::Duration::from_millis(max_poll_ms);
    let start = std::time::Instant::now();

    loop {
        let current = get_current_slot(rpc_client)?;
        if current >= target_slot {
            return Ok(());
        }
        if start.elapsed() > deadline {
            anyhow::bail!("timed out waiting for slot {target_slot} (current={current})");
        }
        tokio::time::sleep(poll_interval).await;
    }
}
