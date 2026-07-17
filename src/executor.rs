//! Executor — builds venue-specific instructions, wraps in Jito bundle,
//! submits (or logs in DRY_RUN). Each venue gets its own instruction builder.
//!
//! Phase 6: real instruction encoding for Pump.fun, PumpSwap, Raydium.
//! Phase 8: live-submit path with Jito bundles.
//!
//! SAFETY NOTE (per Section 8 Phase 6):
//! Instruction encoding is the highest-risk part of this build. If an
//! encoding cannot be cross-checked against an actual on-chain transaction
//! (via getTransaction with jsonParsed), it is marked `UNVERIFIED`.

use crate::config::Config;
use crate::ingest::{SwapDirection, Venue};
use anyhow::Result;
use serde::{Deserialize, Serialize};
use solana_sdk::instruction::{AccountMeta, Instruction};
use solana_sdk::pubkey::Pubkey;
use solana_sdk::transaction::Transaction;
use std::str::FromStr;
use tokio::sync::mpsc::Receiver;

/// Command from risk module to execute a trade.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ExecCommand {
    pub source_wallet: String,
    pub token_mint: String,
    pub venue: Venue,
    pub direction: SwapDirection,
    pub amount_sol: f64,
    pub simulated_price_sol: f64,
}

// ── Program ID constants ────────────────────────────────────────

const PUMP_FUN_PROGRAM: &str = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P";
const PUMP_SWAP_PROGRAM: &str = "pAMMPxompa13c2qojFgUGSXXysyLLCUmSXwG8M7fKtM";
const RAYDIUM_AMM_V4: &str = "675kPX9MHTjS2zt1qfr1NYyze2V9cWzmRpJnLkzFY7";
const RAYDIUM_CPMM: &str = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP";
const SYSTEM_PROGRAM: &str = "11111111111111111111111111111111";
const TOKEN_PROGRAM: &str = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA";
const ASSOC_TOKEN_PROGRAM: &str = "ATokenGPvbdGVxr1b2hvZbsiqW5xr25B9d8x7UfMtM5E";
const RENT_PROGRAM: &str = "SysvarRent111111111111111111111111111111111";

/// Convert SOL amount to lamports (u64).
fn sol_to_lamports(sol: f64) -> u64 {
    (sol * 1_000_000_000.0) as u64
}

// ── Pump.fun instruction builder ─────────────────────────────────
//
// Pump.fun buy/sell uses program 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P.
//
// Buy discriminator (first 8 bytes): 66063d1201daebea (LE)
//   Derived from SHA256("global:buy")[..8]
// Sell discriminator: 33e685a4017f83ad (LE)
//   Derived from SHA256("global:sell")[..8]
//
// Buy instruction data: discriminator(8) + token_amount(8 LE u64) + max_sol_cost(8 LE u64)
// Sell instruction data: discriminator(8) + token_amount(8 LE u64) + min_sol_return(8 LE u64)

fn build_pump_fun_instruction(
    direction: &SwapDirection,
    token_mint: &str,
    amount_sol: f64,
) -> Result<Instruction> {
    let program_id = Pubkey::from_str(PUMP_FUN_PROGRAM)
        .map_err(|e| anyhow::anyhow!("invalid Pump.fun program ID: {e}"))?;
    let mint = Pubkey::from_str(token_mint)
        .map_err(|e| anyhow::anyhow!("invalid token mint: {e}"))?;

    // Discriminator
    let (discriminator, data_suffix) = match direction {
        SwapDirection::Buy => {
            // discriminator: PUMP_FUN_BUY_DISCRIMINATOR (global:buy)
            // data: amount(8) + max_cost(8)
            let disc: [u8; 8] = [0x66, 0x06, 0x3d, 0x12, 0x01, 0xda, 0xeb, 0xea];
            let amount = 1_000_000u64.to_le_bytes(); // 1 token (placeholder)
            let max_cost = sol_to_lamports(amount_sol * 1.05).to_le_bytes(); // 5% slippage
            let mut data = Vec::with_capacity(24);
            data.extend_from_slice(&disc);
            data.extend_from_slice(&amount);
            data.extend_from_slice(&max_cost);
            (disc, data)
        }
        SwapDirection::Sell => {
            let disc: [u8; 8] = [0x33, 0xe6, 0x85, 0xa4, 0x01, 0x7f, 0x83, 0xad];
            let amount = 1_000_000u64.to_le_bytes();
            let min_return = (sol_to_lamports(amount_sol) / 2).to_le_bytes(); // 50% estimated
            let mut data = Vec::with_capacity(24);
            data.extend_from_slice(&disc);
            data.extend_from_slice(&amount);
            data.extend_from_slice(&min_return);
            (disc, data)
        }
        _ => return Err(anyhow::anyhow!("unsupported swap direction for Pump.fun")),
    };

    // Account layout for Pump.fun buy/sell (9 accounts):
    // [0] user (signer, writable)
    // [1] system program
    // [2] token program
    // [3] associated token program
    // [4] pump fun program
    // [5] token mint
    // [6] bonding curve (pda)
    // [7] bonding curve lp (pda)
    // [8] user token account (writable)
    //
    // UNVERIFIED — account order and PDA derivation need cross-check against on-chain tx
    let accounts = vec![
        AccountMeta::new(Pubkey::from_str(SYSTEM_PROGRAM).unwrap(), false),   // [1] system program
        AccountMeta::new_readonly(Pubkey::from_str(TOKEN_PROGRAM).unwrap(), false), // [2] token program
        AccountMeta::new_readonly(Pubkey::from_str(ASSOC_TOKEN_PROGRAM).unwrap(), false), // [3] ATA prog
        AccountMeta::new_readonly(program_id, false),                        // [4] pump fun prog
        AccountMeta::new(mint, false),                                       // [5] token mint
        // [6][7][8] need real PDAs — cannot derive without fee payer
    ];

    Ok(Instruction {
        program_id,
        accounts,
        data: data_suffix,
    })
}

// ── PumpSwap instruction builder ─────────────────────────────────
// UNVERIFIED — layout needs cross-check against on-chain tx

fn build_pump_swap_instruction(
    _direction: &SwapDirection,
    _token_mint: &str,
    _amount_sol: f64,
) -> Result<Instruction> {
    Err(anyhow::anyhow!("PumpSwap encoding not yet implemented (UNVERIFIED)"))
}

// ── Raydium AMM v4 instruction builder ──────────────────────────
//
// Raydium AMM v4 swap uses program 675kPX9MHTjS2zt1qfr1NYyze2V9cWzmRpJnLkzFY7.
// Instruction index: 9 (swap)
// Data format: instruction(1 byte) + amount_in(8 LE u64) + min_amount_out(8 LE u64)

fn build_raydium_amm_v4_instruction(
    direction: &SwapDirection,
    _token_mint: &str,
    amount_sol: f64,
) -> Result<Instruction> {
    let program_id = Pubkey::from_str(RAYDIUM_AMM_V4)
        .map_err(|e| anyhow::anyhow!("invalid Raydium AMM v4 program ID: {e}"))?;

    // Swap instruction data: 0x09 + amount_in(8) + min_amount_out(8)
    let amount_in = sol_to_lamports(amount_sol).to_le_bytes();
    let min_amount_out = 0u64.to_le_bytes(); // no min output (high slippage)

    let mut data = Vec::with_capacity(17);
    data.push(0x09); // instruction index 9 = swap
    data.extend_from_slice(&amount_in);
    data.extend_from_slice(&min_amount_out);

    // Account list for Raydium AMM v4 swap (~18 accounts):
    // [0]  amm (writable)
    // [1]  amm_authority (pda)
    // [2]  open_orders (pda)
    // [3]  lp_mint
    // [4]  coin_mint / token_A
    // [5]  pc_mint / token_B (SOL = So11111111111111111111111111111111111111112)
    // [6]  coin_vault
    // [7]  pc_vault
    // [8]  market_program
    // [9]  market
    // [10] bid
    // [11] ask
    // [12] event_q
    // [13] coin_wallet (user's token ATA)
    // [14] pc_wallet (user's token ATA)
    // [15] user (signer)
    // [16] ... more depending on market
    //
    // UNVERIFIED — account list depends on the specific AMM pool and market
    let accounts = vec![
        AccountMeta::new(Pubkey::from_str(SYSTEM_PROGRAM).unwrap(), false),
        AccountMeta::new_readonly(Pubkey::from_str(TOKEN_PROGRAM).unwrap(), false),
        AccountMeta::new_readonly(Pubkey::from_str(RENT_PROGRAM).unwrap(), false),
    ];

    Ok(Instruction {
        program_id,
        accounts,
        data,
    })
}

// ── Raydium CPMM instruction builder ────────────────────────────
// UNVERIFIED — layout needs cross-check against on-chain tx

fn build_raydium_cpmm_instruction(
    _direction: &SwapDirection,
    _token_mint: &str,
    _amount_sol: f64,
) -> Result<Instruction> {
    Err(anyhow::anyhow!("Raydium CPMM encoding not yet implemented (UNVERIFIED)"))
}

/// Build an instruction for the given venue.
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

// ── Jito bundle submission ──────────────────────────────────────

fn build_jito_bundle(transactions: Vec<Transaction>) -> Result<Vec<Transaction>> {
    Ok(transactions)
}

fn estimate_tip(_cfg: &Config) -> u64 {
    1000
}

// ── Spawn function ──────────────────────────────────────────────

/// Spawn the executor task.
pub fn spawn(cfg: Config, mut exec_rx: Receiver<ExecCommand>) -> tokio::task::JoinHandle<()> {
    tokio::spawn(async move {
        tracing::info!(
            "[executor] starting — DRY_RUN={}, LIVE={}",
            cfg.dry_run, cfg.live
        );

        while let Some(cmd) = exec_rx.recv().await {
            tracing::info!(
                "[executor] received command — {} {} via {:?}",
                cmd.source_wallet, cmd.token_mint, cmd.venue
            );

            if cfg.dry_run {
                tracing::info!(
                    "[executor] DRY_RUN — would execute: {:?} | {} | {:.6} SOL",
                    cmd.venue, cmd.token_mint, cmd.amount_sol
                );
                continue;
            }

            if !cfg.live {
                tracing::warn!("[executor] LIVE=false — short-circuiting real execution");
                continue;
            }

            match build_instruction(&cmd.venue, &cmd.direction, &cmd.token_mint, cmd.amount_sol) {
                Ok(ix) => {
                    tracing::info!(
                        "[executor] built instruction for {:?}: {} accounts, {} bytes data",
                        cmd.venue, ix.accounts.len(), ix.data.len()
                    );
                    // Phase 8: sign + bundle + submit
                }
                Err(e) => {
                    tracing::error!("[executor] failed to build instruction: {e}");
                }
            }
        }

        tracing::info!("[executor] command receiver closed — shutting down");
    })
}
