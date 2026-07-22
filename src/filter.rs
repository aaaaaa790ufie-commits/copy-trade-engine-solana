//! Filter: consumes SwapEvent + current wallet tier from SQLite.

use std::time::Instant;

use crate::config::Config;
use crate::ingest::SwapEvent;
use serde::Serialize;
use tokio::sync::mpsc::{Receiver, Sender};

#[derive(Debug, Clone, Serialize)]
pub struct Decision {
    pub swap: SwapEvent,
    pub should_copy: bool,
    pub reason: String,
    pub wallet_tier: String,
    pub edge_score: f64,
}

struct TierCache {
    tiers: std::collections::HashMap<String, (String, f64)>,
    last_refresh: Instant,
    refresh_interval: std::time::Duration,
    db_path: String,
}

impl TierCache {
    fn refresh(&mut self) {
        self.last_refresh = Instant::now();
        let before = self.tiers.len();
        match rusqlite::Connection::open(&self.db_path) {
            Ok(conn) => {
                let mut stmt = match conn.prepare(
                    "SELECT wallet_address, tier, edge_score FROM wallet_scores",
                ) {
                    Ok(stmt) => stmt,
                    Err(error) => {
                        tracing::warn!("[filter] SQLite prepare failed: {error}");
                        return;
                    }
                };
                let rows = match stmt.query_map([], |row| {
                    Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?, row.get::<_, f64>(2)?))
                }) {
                    Ok(rows) => rows,
                    Err(error) => {
                        tracing::warn!("[filter] SQLite query failed: {error}");
                        return;
                    }
                };
                self.tiers.clear();
                for row in rows.flatten() {
                    self.tiers.insert(row.0, (row.1, row.2));
                }
                tracing::info!(
                    "[filter] tier cache refreshed: {} entries (was {})",
                    self.tiers.len(),
                    before
                );
            }
            Err(error) => tracing::warn!("[filter] cannot open {}: {error}", self.db_path),
        }
    }
}

pub fn spawn(
    _cfg: Config,
    mut swap_rx: Receiver<SwapEvent>,
    decision_tx: Sender<Decision>,
) -> tokio::task::JoinHandle<()> {
    tokio::spawn(async move {
        tracing::info!("[filter] starting");
        let refresh_interval = std::time::Duration::from_secs(30);
        let mut cache = TierCache {
            tiers: std::collections::HashMap::new(),
            // Force a DB read when the first signal arrives. Previously the
            // first 30 seconds of signals were classified as Tier C and lost.
            last_refresh: Instant::now() - refresh_interval,
            refresh_interval,
            db_path: std::env::var("SENTINEL_DB").unwrap_or_else(|_| "sentinel.db".into()),
        };

        while let Some(swap) = swap_rx.recv().await {
            if cache.last_refresh.elapsed() >= cache.refresh_interval {
                cache.refresh();
            }
            let (tier, edge_score) = cache
                .tiers
                .get(&swap.source_wallet)
                .cloned()
                .unwrap_or_else(|| ("C".to_string(), 0.0));
            let (should_copy, reason) = match tier.as_str() {
                "A" => (true, format!("GMGN qualified: 30d winrate={:.1}%", edge_score * 100.0)),
                "B" => (false, format!("Tier B watch-only: score={edge_score:.3}")),
                _ => (false, "Wallet does not meet the GMGN thresholds".to_string()),
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
