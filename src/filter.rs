//! Filter — consumes SwapEvent + current wallet tier from SQLite,
//! decides copy/skip based on strategy config.

use std::time::Instant;

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
                    "SELECT wallet_address, tier, edge_score FROM wallet_scores"
                ) {
                    Ok(s) => s,
                    Err(e) => {
                        tracing::warn!("[filter] SQLite prepare failed: {e}");
                        return;
                    }
                };
                let rows = match stmt.query_map([], |row| {
                    let addr: String = row.get(0)?;
                    let tier: String = row.get(1)?;
                    let edge: f64 = row.get(2)?;
                    Ok((addr, tier, edge))
                }) {
                    Ok(r) => r,
                    Err(e) => {
                        tracing::warn!("[filter] SQLite query failed: {e}");
                        return;
                    }
                };

                self.tiers.clear();
                for row in rows.flatten() {
                    self.tiers.insert(row.0, (row.1, row.2));
                }
                tracing::info!(
                    "[filter] tier cache refreshed — {} entries (was {})",
                    self.tiers.len(), before
                );
            }
            Err(e) => {
                tracing::warn!("[filter] cannot open SQLite {p}: {e}", p = self.db_path);
            }
        }
    }
}

/// Spawn the filter task.
pub fn spawn(
    cfg: Config,
    mut swap_rx: Receiver<SwapEvent>,
    decision_tx: Sender<Decision>,
) -> tokio::task::JoinHandle<()> {
    tokio::spawn(async move {
        tracing::info!("[filter] starting");

        // Ensure the wallet_scores table exists
        if let Ok(conn) = rusqlite::Connection::open("sentinel.db") {
            conn.execute_batch(
                "CREATE TABLE IF NOT EXISTS wallet_scores (
                    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    wallet_address TEXT NOT NULL UNIQUE,
                    tier TEXT NOT NULL DEFAULT 'C',
                    edge_score REAL NOT NULL DEFAULT 0.0
                );
                -- Seed seed wallets as Tier A if table was just created
                INSERT OR IGNORE INTO wallet_scores (wallet_address, tier, edge_score)
                VALUES
                    ('5tzFkiKscXHK5ZXCGbXZxwQBwwiDmP3p1WAMEREbmwBK', 'A', 1.0),
                    ('DRpbwCxPqvNsKGMNchPkBLFxDSrGPzau7kRbnvjyYvK', 'A', 1.0),
                    ('F6UoN7AoUCcWMctBE26E1BQrYGEk8GnGPAhq8aY9X3eK', 'A', 1.0),
                    ('GjEtGzHafgEWsUF3WVqCjYLczHGB1hLrYjhPJ7CoynJp', 'A', 1.0);"
            ).unwrap_or_else(|e| tracing::warn!("[filter] schema init: {e}"));
        }

        let mut cache = TierCache {
            tiers: std::collections::HashMap::new(),
            last_refresh: Instant::now(),
            refresh_interval: std::time::Duration::from_secs(30),
            db_path: "sentinel.db".to_string(),
        };

        while let Some(swap) = swap_rx.recv().await {
            if cache.last_refresh.elapsed() >= cache.refresh_interval {
                cache.refresh();
            }

            let (tier, edge_score) = cache.tiers
                .get(&swap.source_wallet)
                .cloned()
                .unwrap_or_else(|| ("C".to_string(), 0.0));

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
