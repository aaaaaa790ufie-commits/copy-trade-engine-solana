//! Risk — position sizing, exposure caps, token security pre-check
//! (LP lock, mint authority, freeze authority, top-10 holder %).
//! Consumes filter Decisions, produces ExecCommands.

use crate::config::Config;
use crate::executor::ExecCommand;
use crate::filter::Decision;
use tokio::sync::mpsc::{Receiver, Sender};

/// Spawn the risk task.
pub fn spawn(
    cfg: Config,
    mut decision_rx: Receiver<Decision>,
    exec_tx: Sender<ExecCommand>,
) -> tokio::task::JoinHandle<()> {
    tokio::spawn(async move {
        tracing::info!("[risk] starting");

        let mut open_positions: u32 = 0;
        let mut per_source_alloc: std::collections::HashMap<String, f64> = std::collections::HashMap::new();

        while let Some(decision) = decision_rx.recv().await {
            if !decision.should_copy {
                tracing::debug!("[risk] skip — filter rejected: {}", decision.reason);
                continue;
            }

            // ── Check max concurrent positions ────────────────────
            if open_positions >= cfg.risk.max_concurrent_positions {
                tracing::warn!(
                    "[risk] max concurrent positions reached ({}) — skipping {}",
                    open_positions, decision.swap.source_wallet
                );
                continue;
            }

            // ── Check per-source-wallet allocation cap ────────────
            let source = decision.swap.source_wallet.clone();
            let source_alloc = per_source_alloc.get(&source).copied().unwrap_or(0.0);
            if source_alloc >= cfg.risk.max_per_source_wallet_pct {
                tracing::warn!(
                    "[risk] source wallet allocation cap ({:.1}%) reached for {}",
                    cfg.risk.max_per_source_wallet_pct, source
                );
                continue;
            }

            // ── Security pre-check (TODO Phase 5) ─────────────────
            // Would call getAccountInfo on the mint to verify:
            //   - LP burned/locked
            //   - Mint authority renounced
            //   - Freeze authority renounced
            //   - Top-10 holder concentration < config.risk.security.max_top10_holder_pct

            // For now, assume pass (paper mode)
            let security_ok = true;

            if !security_ok {
                tracing::warn!("[risk] security check failed for {}", decision.swap.token_mint);
                continue;
            }

            // ── Compute position size ─────────────────────────────
            // Percent of balance allocated (config.risk.max_allocation_pct)
            // In paper mode, assume a fixed paper balance
            let position_size_sol = 0.01 * cfg.risk.max_allocation_pct; // dummy: 2% of 1 SOL paper

            // ── Send to executor ──────────────────────────────────
            let cmd = ExecCommand {
                source_wallet: source.clone(),
                token_mint: decision.swap.token_mint.clone(),
                venue: decision.swap.venue.clone(),
                direction: decision.swap.direction.clone(),
                amount_sol: position_size_sol,
                simulated_price_sol: decision.swap.price_sol,
            };

            if exec_tx.send(cmd).await.is_err() {
                tracing::error!("[risk] executor receiver dropped");
                break;
            }

            open_positions += 1;
            *per_source_alloc.entry(source.clone()).or_insert(0.0) += position_size_sol;

            tracing::info!(
                "[risk] approved copy — {} {} {:.4} SOL via {:?} (open={})",
                source,
                decision.swap.token_mint,
                position_size_sol,
                decision.swap.venue,
                open_positions
            );
        }
    })
}
