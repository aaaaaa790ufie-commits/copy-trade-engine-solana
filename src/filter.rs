//! Filter — consumes SwapEvent + current wallet tier from SQLite,
//! decides copy/skip based on strategy config.

use crate::config::Config;
use crate::ingest::SwapEvent;
use serde::Serialize;
use tokio::sync::mpsc::{Receiver, Sender};

/// Outcome of the filter decision for a SwapEvent.
#[derive(Debug, Clone, Serialize)]
pub struct Decision {
    pub swap: SwapEvent,
    pub should_copy: bool,
    pub reason: String,
    pub wallet_tier: String,
    pub edge_score: f64,
}

/// In-memory cache of wallet tiers, refreshed from SQLite on an interval.
struct TierCache {
    // wallet_address -> (tier, edge_score)
    tiers: std::collections::HashMap<String, (String, f64)>,
    last_refresh: std::time::Instant,
    refresh_interval: std::time::Duration,
}

/// Spawn the filter task.
pub fn spawn(
    _cfg: Config,
    mut swap_rx: Receiver<SwapEvent>,
    decision_tx: Sender<Decision>,
) -> tokio::task::JoinHandle<()> {
    tokio::spawn(async move {
        tracing::info!("[filter] starting");

        let mut cache = TierCache {
            tiers: std::collections::HashMap::new(),
            last_refresh: std::time::Instant::now(),
            refresh_interval: std::time::Duration::from_secs(30),
        };

        while let Some(swap) = swap_rx.recv().await {
            // Refresh tier cache periodically (not on every swap — hot path)
            if cache.last_refresh.elapsed() >= cache.refresh_interval {
                // TODO Phase 5: read from SQLite (scorer output)
                tracing::debug!("[filter] refreshing tier cache from SQLite");
                cache.last_refresh = std::time::Instant::now();
            }

            // Look up the source wallet's tier
            let (tier, edge_score) = cache.tiers
                .get(&swap.source_wallet)
                .cloned()
                .unwrap_or_else(|| ("C".to_string(), 0.0));

            // Decision logic
            let (should_copy, reason) = match tier.as_str() {
                "A" => (true, format!("Tier A — edge_score={:.3}", edge_score)),
                "B" => (false, format!("Tier B — watch-only, edge_score={:.3}", edge_score)),
                _ => (false, "Tier C — not tracked".to_string()),
            };

            let decision = Decision {
                swap,
                should_copy,
                reason,
                wallet_tier: tier,
                edge_score,
            };

            if decision_tx.send(decision).await.is_err() {
                tracing::error!("[filter] decision receiver dropped");
                break;
            }
        }
    })
}
