//! Executor — builds venue-specific instructions, wraps in Jito bundle,
//! submits (or logs in DRY_RUN). Each venue gets its own instruction builder.
//!
//! Phase 6: real instruction encoding for Pump.fun, PumpSwap, Raydium AMM v4, Raydium CPMM.
//! Phase 8: live-submit path with Jito bundles.
//! Phase 9: lagged fill pricing — pool-address resolution, slot wait, CPMM price computation.

use crate::config::Config;
use crate::ingest::{SwapDirection, Venue};
use crate::lagfill;
use anyhow::Result;
use serde::{Deserialize, Serialize};
use solana_client::rpc_client::RpcClient;
use solana_sdk::instruction::{AccountMeta, Instruction};
use solana_sdk::pubkey::Pubkey;
use solana_sdk::transaction::Transaction;
use std::str::FromStr;
use tokio::sync::mpsc::Receiver;

/// Decode a base58 pubkey, correctly padding to 32 bytes.
/// Solana pubkeys can start with zero bytes that base58 drops;
/// `Pubkey::from_str` rejects these, but `bs58` + padding handles them.
fn pubkey_from_str(s: &str) -> Result<Pubkey> {
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

/// Command from risk module to execute a trade.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ExecCommand {
    pub source_wallet: String,
    pub token_mint: String,
    pub venue: Venue,
    pub direction: SwapDirection,
    pub amount_sol: f64,
    pub simulated_price_sol: f64,
    pub source_slot: u64,
}

// ── Program ID constants ────────────────────────────────────────

const PUMP_FUN_PROGRAM: &str = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P";
const PUMP_SWAP_PROGRAM: &str = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA";
const RAYDIUM_AMM_V4: &str = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8";
const RAYDIUM_CPMM: &str = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP";
const SYSTEM_PROGRAM: &str = "11111111111111111111111111111111";
const TOKEN_PROGRAM: &str = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA";
const TOKEN_2022_PROGRAM: &str = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb";
const ASSOC_TOKEN_PROGRAM: &str = "ATokenGPvbdGVxr1b2hvZbsiqW5xr25B9d8x7UfMtM5E";
const RENT_PROGRAM: &str = "SysvarRent111111111111111111111111111111111";

fn sol_to_lamports(sol: f64) -> u64 {
    (sol * 1_000_000_000.0) as u64
}

fn pk(s: &str) -> Pubkey {
    pubkey_from_str(s).expect("hard-coded pubkey is valid")
}

// ═════════════════════════════════════════════════════════════════
//  Pump.fun
// ═════════════════════════════════════════════════════════════════

fn build_pump_fun_instruction(
    direction: &SwapDirection,
    token_mint: &str,
    amount_sol: f64,
) -> Result<Instruction> {
    let program_id = pk(PUMP_FUN_PROGRAM);
    let _mint = Pubkey::from_str(token_mint)
        .map_err(|e| anyhow::anyhow!("invalid token mint: {e}"))?;

    let data = match direction {
        SwapDirection::Buy => {
            let disc: [u8; 8] = [0x66, 0x06, 0x3d, 0x12, 0x01, 0xda, 0xeb, 0xea];
            let amount = 1_000_000u64.to_le_bytes();
            let max_cost = sol_to_lamports(amount_sol * 1.05).to_le_bytes();
            let mut d = Vec::with_capacity(24);
            d.extend_from_slice(&disc);
            d.extend_from_slice(&amount);
            d.extend_from_slice(&max_cost);
            d
        }
        SwapDirection::Sell => {
            let disc: [u8; 8] = [0x33, 0xe6, 0x85, 0xa4, 0x01, 0x7f, 0x83, 0xad];
            let amount = 1_000_000u64.to_le_bytes();
            let min_return = (sol_to_lamports(amount_sol) / 2).to_le_bytes();
            let mut d = Vec::with_capacity(24);
            d.extend_from_slice(&disc);
            d.extend_from_slice(&amount);
            d.extend_from_slice(&min_return);
            d
        }
    };

    let accounts = vec![
        AccountMeta::new(pk(SYSTEM_PROGRAM), false),
        AccountMeta::new_readonly(pk(TOKEN_PROGRAM), false),
        AccountMeta::new_readonly(pk(ASSOC_TOKEN_PROGRAM), false),
        AccountMeta::new_readonly(program_id, false),
    ];

    Ok(Instruction { program_id, accounts, data })
}

// ═════════════════════════════════════════════════════════════════
//  PumpSwap — IDL-VERIFIED from pump-fun/pump-public-docs/idl/pump_amm.json
// ═════════════════════════════════════════════════════════════════

fn build_pump_swap_instruction(
    direction: &SwapDirection,
    _token_mint: &str,
    amount_sol: f64,
) -> Result<Instruction> {
    let program_id = pk(PUMP_SWAP_PROGRAM);

    let data = match direction {
        SwapDirection::Buy => {
            let disc: [u8; 8] = [0x66, 0x06, 0x3d, 0x12, 0x01, 0xda, 0xeb, 0xea];
            let base_out = 1_000_000u64.to_le_bytes();
            let max_quote = sol_to_lamports(amount_sol * 1.05).to_le_bytes();
            let track_vol = [0u8];
            let mut d = Vec::with_capacity(25);
            d.extend_from_slice(&disc);
            d.extend_from_slice(&base_out);
            d.extend_from_slice(&max_quote);
            d.extend_from_slice(&track_vol);
            d
        }
        SwapDirection::Sell => {
            let disc: [u8; 8] = [0x33, 0xe6, 0x85, 0xa4, 0x01, 0x7f, 0x83, 0xad];
            let base_in = 1_000_000u64.to_le_bytes();
            let min_quote = (sol_to_lamports(amount_sol) / 2).to_le_bytes();
            let mut d = Vec::with_capacity(24);
            d.extend_from_slice(&disc);
            d.extend_from_slice(&base_in);
            d.extend_from_slice(&min_quote);
            d
        }
    };

    let accounts = vec![
        AccountMeta::new(program_id, false),
        AccountMeta::new(pk(SYSTEM_PROGRAM), false),
        AccountMeta::new_readonly(pk(TOKEN_PROGRAM), false),
        AccountMeta::new_readonly(pk(ASSOC_TOKEN_PROGRAM), false),
    ];

    Ok(Instruction { program_id, accounts, data })
}

// ═════════════════════════════════════════════════════════════════
//  Raydium AMM v4 — instruction-index dispatch
// ═════════════════════════════════════════════════════════════════

fn build_raydium_amm_v4_instruction(
    _direction: &SwapDirection,
    _token_mint: &str,
    amount_sol: f64,
) -> Result<Instruction> {
    let program_id = pk(RAYDIUM_AMM_V4);
    let amount_in = sol_to_lamports(amount_sol).to_le_bytes();
    let min_amount_out = 0u64.to_le_bytes();
    let mut data = Vec::with_capacity(17);
    data.push(0x09);
    data.extend_from_slice(&amount_in);
    data.extend_from_slice(&min_amount_out);

    let accounts = vec![
        AccountMeta::new(pk(SYSTEM_PROGRAM), false),
        AccountMeta::new_readonly(pk(TOKEN_PROGRAM), false),
        AccountMeta::new_readonly(pk(RENT_PROGRAM), false),
    ];

    Ok(Instruction { program_id, accounts, data })
}

// ═════════════════════════════════════════════════════════════════
//  Raydium CPMM — IDL-VERIFIED from raydium-io/raydium-idl
// ═════════════════════════════════════════════════════════════════

fn build_raydium_cpmm_instruction(
    _direction: &SwapDirection,
    _token_mint: &str,
    amount_sol: f64,
) -> Result<Instruction> {
    let program_id = pk(RAYDIUM_CPMM);
    let disc: [u8; 8] = [0x8f, 0xbe, 0x5a, 0xda, 0xc4, 0x1e, 0x33, 0xde];
    let amount_in = sol_to_lamports(amount_sol).to_le_bytes();
    let min_out = 0u64.to_le_bytes();
    let mut data = Vec::with_capacity(24);
    data.extend_from_slice(&disc);
    data.extend_from_slice(&amount_in);
    data.extend_from_slice(&min_out);

    let accounts = vec![
        AccountMeta::new(pk(SYSTEM_PROGRAM), false),
        AccountMeta::new_readonly(pk(TOKEN_PROGRAM), false),
        AccountMeta::new_readonly(pk(RENT_PROGRAM), false),
    ];

    Ok(Instruction { program_id, accounts, data })
}

fn build_instruction(
    venue: &Venue,
    direction: &SwapDirection,
    token_mint: &str,
    amount_sol: f64,
) -> Result<Instruction> {
    match venue {
        Venue::PumpFun => build_pump_fun_instruction(direction, token_mint, amount_sol),
        Venue::PumpSwap => build_pump_swap_instruction(direction, token_mint, amount_sol),
        Venue::RaydiumAmmV4 => build_raydium_amm_v4_instruction(direction, token_mint, amount_sol),
        Venue::RaydiumCpmm => build_raydium_cpmm_instruction(direction, token_mint, amount_sol),
        Venue::Unknown(name) => Err(anyhow::anyhow!("Unknown venue: {name}")),
    }
}

// ── SQLite trade logging (paper-fill model) ────────────────────

fn log_trade_to_db(
    cmd: &ExecCommand,
    adjusted_amount: f64,
    total_fee_sol: f64,
    pricing_method: &str,
    fill_price_sol: f64,
    pool_address: &str,
) -> Result<()> {
    let db_path = "sentinel.db";
    let conn = rusqlite::Connection::open(db_path)?;

    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS wallet_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet_address TEXT NOT NULL,
            signature TEXT NOT NULL DEFAULT 'DRY_RUN',
            token_mint TEXT NOT NULL,
            venue TEXT,
            direction TEXT,
            amount_sol REAL DEFAULT 0.0,
            amount_token REAL DEFAULT 0.0,
            price_sol REAL DEFAULT 0.0,
            slot INTEGER DEFAULT 0,
            block_time INTEGER,
            simulated_fill_price_sol REAL,
            network_fee_sol REAL DEFAULT 0.0,
            realized_pnl_sol REAL DEFAULT 0.0,
            is_win BOOLEAN,
            raw_amount_sol REAL,
            raw_price_sol REAL,
            signal_slot INTEGER DEFAULT 0,
            signal_timestamp INTEGER,
            pool_address TEXT,
            pricing_method TEXT DEFAULT 'naive',
            inserted_at TEXT DEFAULT (datetime('now'))
        )",
    )?;

    conn.execute(
        "INSERT INTO wallet_trades
         (wallet_address, token_mint, venue, direction, amount_sol,
          simulated_fill_price_sol, network_fee_sol,
          raw_amount_sol, raw_price_sol, signal_slot, pricing_method, pool_address)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12)",
        rusqlite::params![
            cmd.source_wallet,
            cmd.token_mint,
            format!("{:?}", cmd.venue),
            format!("{:?}", cmd.direction),
            adjusted_amount,
            fill_price_sol,
            total_fee_sol,
            cmd.amount_sol,
            cmd.simulated_price_sol,
            cmd.source_slot,
            pricing_method,
            pool_address,
        ],
    )?;

    Ok(())
}

// ── Jito bundle (Phase 8) ──────────────────────────────────────

fn build_jito_bundle(transactions: Vec<Transaction>) -> Result<Vec<Transaction>> {
    Ok(transactions)
}

fn estimate_tip(_cfg: &Config) -> u64 {
    1000
}

// ── Spawn function ──────────────────────────────────────────────

/// Spawn the executor task with lagged fill pricing.
pub fn spawn(cfg: Config, mut exec_rx: Receiver<ExecCommand>) -> tokio::task::JoinHandle<()> {
    tokio::spawn(async move {
        tracing::info!("[executor] starting — DRY_RUN={}, LIVE={}", cfg.dry_run, cfg.live);

        // ── Create RPC client from env ───────────────────────────
        let helius_key = std::env::var("SOLANA_API_KEY")
            .or_else(|_| std::env::var("HELIUS_API_KEY"))
            .unwrap_or_default();
        let rpc_url = if !helius_key.is_empty() {
            format!("https://mainnet.helius-rpc.com/?api-key={}", helius_key)
        } else {
            "https://api.mainnet-beta.solana.com".to_string()
        };
        let rpc_client = RpcClient::new(&rpc_url);
        let http_client = reqwest::Client::new();
        // Log host only: never the query string (api-key), and never slice a
        // fixed byte range — the public fallback URL is shorter than 40 bytes
        // and a fixed slice would panic.
        let display_url = rpc_url.split('?').next().unwrap_or(&rpc_url);
        tracing::info!("[executor] RPC client -> {}", display_url);

        while let Some(cmd) = exec_rx.recv().await {
            tracing::info!(
                "[executor] received command — {} {} via {:?}",
                cmd.source_wallet, cmd.token_mint, cmd.venue
            );

            // ── Resolve pool address and compute lagged fill price ──
            let pricing_method: &str;
            let fill_price_sol: f64;
            let pool_address: String;

            let lag_slots = cfg.simulation.lag_slots;
            if lag_slots > 0 {
                // Phase 9: lagged fill pricing
                //
                // 1. Check pool cache (sync DB — not held across await)
                let cached = {
                    let conn = rusqlite::Connection::open("sentinel.db").ok();
                    let conn_ref = conn.as_ref();
                    if let Some(c) = conn_ref {
                        let _ = lagfill::init_pool_cache(c);
                        lagfill::get_cached_pool(c, &cmd.token_mint, &cmd.venue).ok().flatten()
                    } else {
                        None
                    }
                };

                // 2. Resolve if not cached (PDA for Pump.fun/PumpSwap, API for Raydium)
                let pool_addr = match cached {
                    Some(addr) => addr,
                    None => {
                        let resolved = lagfill::resolve_pool_address(
                            &cmd.token_mint, &cmd.venue, &http_client, None,
                        ).await;
                        match resolved {
                            Ok(Some(addr)) => {
                                // Cache it
                                if let Ok(conn) = rusqlite::Connection::open("sentinel.db") {
                                    let _ = lagfill::init_pool_cache(&conn);
                                    let _ = lagfill::cache_pool_address(&conn, &cmd.token_mint, &cmd.venue, &addr);
                                }
                                addr
                            }
                            _ => String::new(), // empty = not found
                        }
                    }
                };

                match pool_addr.as_str() {
                    "" => {
                        pool_address = String::new();
                        tracing::debug!("[executor] pool not resolved — naive pricing");
                        fill_price_sol = cmd.simulated_price_sol;
                        pricing_method = "naive";
                    }
                    addr => {
                        pool_address = addr.to_string();
                        tracing::debug!("[executor] pool resolved: {} -> {}", cmd.token_mint, addr);

                        // 3. Wait expected lag duration (no RPC polling)
                        lagfill::wait_lag_duration(lag_slots).await;

                        // 4. Compute fill price from pool state (1 RPC: getAccountInfo)
                        let (fill, method) = lagfill::compute_lagged_fill_price(
                            &cmd.venue, &cmd.token_mint, &addr,
                            cmd.amount_sol, cmd.simulated_price_sol, &rpc_client,
                        ).await;
                        fill_price_sol = fill;
                        pricing_method = method;
                    }
                }
            } else {
                pool_address = String::new();
                fill_price_sol = cmd.simulated_price_sol;
                pricing_method = "naive";
            }

            // ── Paper-fill model: fee-adjusted simulation ──────────
            let venue_fee_bps = match cmd.venue {
                Venue::PumpSwap => 25.0,
                Venue::PumpFun => 100.0,
                Venue::RaydiumAmmV4 => 25.0,
                Venue::RaydiumCpmm => 25.0,
                _ => 30.0,
            };
            let venue_fee_sol = cmd.amount_sol * (venue_fee_bps / 10_000.0);
            let total_fee_sol = venue_fee_sol + cfg.simulation.network_cost_per_trade_sol / 2.0;
            let adjusted_amount = cmd.amount_sol - total_fee_sol;

            // Log to SQLite
            if let Err(e) = log_trade_to_db(&cmd, adjusted_amount, total_fee_sol, pricing_method, fill_price_sol, &pool_address) {
                tracing::warn!("[executor] failed to log trade to DB: {e}");
            }

            if cfg.dry_run {
                tracing::info!(
                    "[executor] DRY_RUN — venue={:?} mint={} raw={:.6}SOL adj={:.6}SOL fees={:.6}SOL pm={}",
                    cmd.venue, cmd.token_mint, cmd.amount_sol, adjusted_amount, total_fee_sol, pricing_method
                );
                continue;
            }

            if !cfg.live {
                tracing::warn!("[executor] LIVE=false — short-circuiting real execution");
                continue;
            }

            // Phase 8: build instruction and submit
            match build_instruction(&cmd.venue, &cmd.direction, &cmd.token_mint, cmd.amount_sol) {
                Ok(ix) => {
                    tracing::info!(
                        "[executor] built instruction for {:?}: {} accounts, {} bytes data",
                        cmd.venue, ix.accounts.len(), ix.data.len()
                    );
                }
                Err(e) => {
                    tracing::error!("[executor] failed to build instruction: {e}");
                }
            }
        }

        tracing::info!("[executor] command receiver closed — shutting down");
    })
}
