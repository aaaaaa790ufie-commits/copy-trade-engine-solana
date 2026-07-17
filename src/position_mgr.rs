//! Position Manager — tracks open positions, applies TP/SL/trailing stop,
//! triggers auto-sell through executor.

use crate::config::Config;

/// Represents one open position in memory (mirrored in SQLite).
#[derive(Debug, Clone)]
struct Position {
    token_mint: String,
    entry_price_sol: f64,
    amount_sol: f64,
    peak_price_sol: f64,
    source_wallet: String,
    opened_at_slot: u64,
}

/// Spawn the position manager task.
pub fn spawn(cfg: Config) -> tokio::task::JoinHandle<()> {
    tokio::spawn(async move {
        tracing::info!("[position_mgr] starting");

        let positions: Vec<Position> = Vec::new();
        let check_interval = tokio::time::Duration::from_secs(cfg.position_manager.check_interval_seconds);

        loop {
            tokio::time::sleep(check_interval).await;

            if positions.is_empty() {
                continue;
            }

            // TODO Phase 7:
            // For each open position:
            //   1. Fetch current token price (via RPC pool / pool state)
            //   2. Check stop-loss: current_price <= entry_price * (1 + stop_loss_pct/100)
            //   3. Update peak_price if current > peak
            //   4. Check trailing stop: if peak >= entry * (1 + trailing_activate_pct/100)
            //      and current <= peak * (1 - trailing_distance_pct/100) -> sell
            //   5. Trigger auto-sell via executor channel
            //   6. Remove position from vec

            // For now, just log placeholder
            if !cfg.dry_run {
                tracing::debug!("[position_mgr] checking {} positions", positions.len());
            }
        }
    })
}
