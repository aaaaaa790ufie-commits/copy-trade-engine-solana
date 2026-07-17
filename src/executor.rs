//! Executor — builds venue-specific instructions, wraps in Jito bundle,
//! submits (or logs in DRY_RUN). Each venue gets its own instruction builder.
//!
//! Phase 6: real instruction encoding for Pump.fun, PumpSwap, Raydium.
//! Phase 8: live-submit path with Jito bundles.
//!
//! SAFETY NOTE (per Section 8 Phase 6):
//! Instruction encoding is the highest-risk part of this build. If an
//! encoding cannot be cross-checked against an actual on-chain transaction
//! (via getTransaction with jsonParsed), it is marked `UNVERIFIED` in both
//! code comments and PROGRESS.md.

use crate::config::Config;
use crate::ingest::{SwapDirection, Venue};
use anyhow::Result;
use serde::{Deserialize, Serialize};
use solana_sdk::instruction::Instruction;
use solana_sdk::transaction::Transaction;
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

// ── Build venue-specific instructions ───────────────────────────
//
// These builders encode the instruction data and account list for
// each DEX program's swap/buy/sell instruction.
//
// Reference: parsed transactions from Solana RPC (`getTransaction`
// with jsonParsed encoding) + public program documentation.

fn build_pump_fun_instruction(
    _direction: &SwapDirection,
    _token_mint: &str,
    _amount_sol: f64,
) -> Result<Instruction> {
    // TODO Phase 6: Pump.fun instruction encoding
    //
    // Pump.fun buy instruction:
    //   - Discriminator: 0x66063d1201daebea (buy) / 0x33e685a4017f83ad (sell)
    //   - Accounts:
    //     [0] fee payer / signer (trading wallet)
    //     [1] system program
    //     [2] token program
    //     [3] associated token account
    //     [4] pump fun program
    //     [5] mint account
    //     [6] bonding curve account
    //     [7] bonding curve LP account
    //   - Data: discriminator (8 bytes) + amount (8 bytes) + max_cost (8 bytes)
    //
    // UNVERIFIED — needs cross-check against on-chain tx

    Err(anyhow::anyhow!("Pump.fun instruction encoding not yet implemented (UNVERIFIED)"))
}

fn build_pump_swap_instruction(
    _direction: &SwapDirection,
    _token_mint: &str,
    _amount_sol: f64,
) -> Result<Instruction> {
    // TODO Phase 6: PumpSwap instruction encoding
    //
    // PumpSwap uses a constant-product AMM similar to Raydium CPMM.
    // Account layout is venue-specific.
    //
    // UNVERIFIED — needs cross-check against on-chain tx

    Err(anyhow::anyhow!("PumpSwap instruction encoding not yet implemented (UNVERIFIED)"))
}

fn build_raydium_amm_v4_instruction(
    _direction: &SwapDirection,
    _token_mint: &str,
    _amount_sol: f64,
) -> Result<Instruction> {
    // TODO Phase 6: Raydium AMM v4 instruction encoding
    //
    // Raydium AMM v4 swap instruction:
    //   - Program: 675kPX9MHTjS2zt1qfr1NYyze2V9cWzmRpJnLkzFY7
    //   - Instruction index: 9 (swap)
    //   - Accounts: amm, amm_authority, open_orders, market_program,
    //     market, serum/coin vault, pc vault, etc.
    //
    // UNVERIFIED — needs cross-check against on-chain tx

    Err(anyhow::anyhow!("Raydium AMM v4 instruction encoding not yet implemented (UNVERIFIED)"))
}

fn build_raydium_cpmm_instruction(
    _direction: &SwapDirection,
    _token_mint: &str,
    _amount_sol: f64,
) -> Result<Instruction> {
    // TODO Phase 6: Raydium CPMM instruction encoding
    //
    // Raydium CPMM uses a constant-product formula.
    // Account layout differs from AMM v4.
    //
    // UNVERIFIED — needs cross-check against on-chain tx

    Err(anyhow::anyhow!("Raydium CPMM instruction encoding not yet implemented (UNVERIFIED)"))
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
        Venue::Unknown(name) => Err(anyhow::anyhow!("Unknown venue: {}", name)),
    }
}

// ── Jito bundle submission ──────────────────────────────────────
//
// Jito bundle submission is free — you only pay the tip (in SOL) as
// part of trade economics, not an infra bill.
// Reference: https://docs.jito.wtf/lowlevelapi/

fn build_jito_bundle(_transactions: Vec<Transaction>) -> Result<Vec<Transaction>> {
    // TODO Phase 8: wrap in Jito bundle
    // 1. Serialize transactions
    // 2. POST to Jito Block Engine endpoint
    //    (https://mainnet.block-engine.jito.wtf/api/v1/bundles)
    // 3. Include tip instruction
    Ok(_transactions)
}

fn estimate_tip(_cfg: &Config) -> u64 {
    // Default tip: config.simulation.jito_tip_per_trade_sol in lamports
    // TODO Phase 8: dynamic tip estimation based on recent bundle activity
    1000 // placeholder: 1000 lamports = 0.000001 SOL
}

// ── Spawn function ──────────────────────────────────────────────

/// Spawn the executor task.
pub fn spawn(cfg: Config, exec_rx: Receiver<ExecCommand>) -> tokio::task::JoinHandle<()> {
    tokio::spawn(async move {
        let _exec_rx = exec_rx; // keep alive (drop when exec not yet needed)
        tracing::info!(
            "[executor] starting — DRY_RUN={}, LIVE={}",
            cfg.dry_run, cfg.live
        );

        // In Phase 4-5, there are no real ExecCommands yet (filter/risk
        // are stubs). The executor is wired to receive commands when the
        // full pipeline is active.

        // TODO Phase 6/8:
        // while let Some(cmd) = exec_rx.recv().await {
        //     // DRY_RUN gate
        //     if cfg.dry_run {
        //         tracing::info!(...);
        //         continue;
        //     }
        //     // LIVE gate
        //     if !cfg.live {
        //         tracing::warn!(...);
        //         continue;
        //     }
        //     // Build instruction
        //     let ix = build_instruction(&cmd.venue, ...)?;
        //     // Build tx
        //     let tx = Transaction::new_unsigned(...);
        //     // Wrap in Jito bundle
        //     let bundle = build_jito_bundle(vec![tx]);
        //     // Submit
        //     // Handle errors
        // }

        loop {
            tokio::time::sleep(tokio::time::Duration::from_secs(60)).await;
            tracing::debug!("[executor] heartbeat — waiting for commands");
        }
    })
}
