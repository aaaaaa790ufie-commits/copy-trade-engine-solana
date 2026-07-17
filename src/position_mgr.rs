//! Position Manager — tracks open positions, applies TP/SL/trailing stop,
//! triggers auto-sell through executor.

use solana_client::rpc_client::RpcClient as SolanaClient;
use solana_sdk::pubkey::Pubkey;
use std::str::FromStr;

use crate::config::Config;
use crate::executor::ExecCommand;
use crate::ingest::{SwapDirection, Venue};
use tokio::sync::mpsc::Sender;

const PUBLIC_RPC: &str = "https://api.mainnet-beta.solana.com";

/// Represents one open position in memory.
#[derive(Debug, Clone)]
struct Position {
    token_mint: String,
    entry_price_sol: f64,
    amount_sol: f64,
    peak_price_sol: f64,
    source_wallet: String,
    opened_at_slot: u64,
    /// How many ticks has this position been open (for activation delay)
    ticks_open: u32,
}

/// Estimate current token price from SOL pair using token balance heuristic.
///
/// Uses the public RPC to fetch account info for the token mint and one
/// known pool to estimate price.  For production, this should use the
/// actual pool state (Raydium/Pump.fun bonding curve).
fn fetch_current_price(token_mint: &str, _amount_sol: f64) -> f64 {
    // TODO Phase 7: real price fetch from pool state
    // For now return a stub price (same as entry)
    tracing::debug!("[position_mgr] price fetch STUB — returning 0.0 for {token_mint}");
    0.0 // stub: caller handles f64::NAN
}

/// Spawn the position manager task.
pub fn spawn(cfg: Config, exec_tx: Sender<ExecCommand>) -> tokio::task::JoinHandle<()> {
    tokio::spawn(async move {
        tracing::info!("[position_mgr] starting");
        let mut positions: Vec<Position> = Vec::new();
        let interval = tokio::time::Duration::from_secs(cfg.position_manager.check_interval_seconds);

        loop {
            tokio::time::sleep(interval).await;

            if positions.is_empty() {
                continue;
            }

            let mut to_remove: Vec<usize> = Vec::new();

            for (i, pos) in positions.iter_mut().enumerate() {
                pos.ticks_open += 1;

                let current_price = fetch_current_price(&pos.token_mint, pos.amount_sol);

                // Skip price if fetch failed
                if current_price <= 0.0 {
                    tracing::warn!(
                        "[position_mgr] cannot fetch price for {} — retrying next tick",
                        pos.token_mint
                    );
                    continue;
                }

                // Update peak price
                if current_price > pos.peak_price_sol {
                    pos.peak_price_sol = current_price;
                }

                // ── Check trailing stop ───────────────────────────
                let sl_cfg = &cfg.risk.stop_loss;

                // Only activate trailing after entry age (ticks)
                let min_ticks = (sl_cfg.trailing_activate_pct as u64 / cfg.position_manager.check_interval_seconds).max(1) as u32;
                if pos.ticks_open >= min_ticks {
                    let peak = pos.peak_price_sol;
                    let entry = pos.entry_price_sol;

                    // Activation threshold: price must have risen enough from entry
                    let activation_price = entry * (1.0 + sl_cfg.trailing_activate_pct / 100.0);
                    if peak >= activation_price {
                        // Trailing distance: if price dropped below peak - distance%
                        let stop_price = peak * (1.0 - sl_cfg.trailing_distance_pct / 100.0);
                        if current_price <= stop_price {
                            tracing::info!(
                                "[position_mgr] trailing stop triggered for {}: peak={:.9} curr={:.9} stop={:.9}",
                                pos.token_mint, peak, current_price, stop_price
                            );
                            to_remove.push(i);
                            continue;
                        }
                    }
                }

                // ── Check stop-loss ───────────────────────────────
                let sl_price = pos.entry_price_sol * (1.0 - sl_cfg.stop_loss_pct / 100.0);
                if current_price <= sl_price {
                    tracing::warn!(
                        "[position_mgr] stop-loss triggered for {}: entry={:.9} curr={:.9} sl={:.9}",
                        pos.token_mint, pos.entry_price_sol, current_price, sl_price
                    );
                    to_remove.push(i);
                    continue;
                }

                // NOTE: take-profit is implicit via trailing stop + manual sell
                // A fixed take-profit % can be added when config supports it
            }

            // ── Handle closed positions (sell signal) ─────────────
            for &i in to_remove.iter().rev() {
                if let Some(pos) = positions.get(i) {
                    if cfg.position_manager.auto_sell_enabled {
                        let cmd = ExecCommand {
                            source_wallet: pos.source_wallet.clone(),
                            token_mint: pos.token_mint.clone(),
                            venue: Venue::Unknown("auto-sell".to_string()),
                            direction: SwapDirection::Sell,
                            amount_sol: pos.amount_sol,
                            simulated_price_sol: 0.0,
                        };
                        if exec_tx.send(cmd).await.is_err() {
                            tracing::error!("[position_mgr] exec_tx send failed");
                        }
                    } else {
                        tracing::debug!("[position_mgr] auto-sell disabled — position closed without sell");
                    }
                }
                positions.remove(i);
            }

            if !to_remove.is_empty() {
                tracing::info!(
                    "[position_mgr] closed {} position(s), {} remaining",
                    to_remove.len(),
                    positions.len()
                );
            }
        }
    })
}
