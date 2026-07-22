//! Ingest: consume qualified Smart Money cluster signals from SQLite.
//!
//! `gmgn/monitor.py` owns discovery, wallet scoring, venue-independent trade
//! detection, and cluster confirmation. This module deliberately does not
//! decode Solana program logs. It only turns pending GMGN signals into the
//! existing in-process `SwapEvent` used by filter -> risk -> executor.

use crate::config::Config;
use rusqlite::{params, Connection};
use serde::{Deserialize, Serialize};
use tokio::sync::mpsc::Sender;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SwapEvent {
    pub source_wallet: String,
    pub token_mint: String,
    pub venue: Venue,
    pub direction: SwapDirection,
    pub amount_sol: f64,
    pub amount_token: f64,
    pub price_sol: f64,
    pub slot: u64,
    pub signature: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum Venue {
    PumpFun,
    PumpSwap,
    RaydiumAmmV4,
    RaydiumCpmm,
    Unknown(String),
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum SwapDirection {
    Buy,
    Sell,
}

#[derive(Debug)]
struct PendingSignal {
    id: i64,
    signal_key: String,
    token_mint: String,
    source_wallet: String,
    venue: String,
    amount_usd: f64,
    price_usd: f64,
    signal_timestamp: i64,
    wallet_count: i64,
    avg_winrate: f64,
}

fn parse_venue(value: &str) -> Venue {
    match value {
        "PumpFun" => Venue::PumpFun,
        "PumpSwap" => Venue::PumpSwap,
        "RaydiumAmmV4" => Venue::RaydiumAmmV4,
        "RaydiumCpmm" => Venue::RaydiumCpmm,
        other => Venue::Unknown(other.to_string()),
    }
}

fn next_signal(db_path: &str) -> rusqlite::Result<Option<PendingSignal>> {
    let conn = Connection::open(db_path)?;
    let mut stmt = conn.prepare(
        "SELECT id, signal_key, token_mint, source_wallet, venue, amount_usd,
                price_usd, signal_timestamp, wallet_count, avg_winrate
         FROM gmgn_signals
         WHERE status = 'pending'
         ORDER BY signal_timestamp ASC
         LIMIT 1",
    )?;

    let signal = stmt
        .query_row([], |row| {
            Ok(PendingSignal {
                id: row.get(0)?,
                signal_key: row.get(1)?,
                token_mint: row.get(2)?,
                source_wallet: row.get(3)?,
                venue: row.get(4)?,
                amount_usd: row.get(5)?,
                price_usd: row.get(6)?,
                signal_timestamp: row.get(7)?,
                wallet_count: row.get(8)?,
                avg_winrate: row.get(9)?,
            })
        })
        .optional()?;

    if let Some(ref row) = signal {
        conn.execute(
            "UPDATE gmgn_signals SET status = 'consumed', consumed_at = datetime('now')
             WHERE id = ? AND status = 'pending'",
            params![row.id],
        )?;
    }
    Ok(signal)
}

use rusqlite::OptionalExtension;

pub fn spawn(_cfg: Config, tx: Sender<SwapEvent>) -> tokio::task::JoinHandle<()> {
    tokio::spawn(async move {
        let db_path = std::env::var("SENTINEL_DB").unwrap_or_else(|_| "sentinel.db".into());
        tracing::info!("[ingest] GMGN signal queue active: {db_path}");

        loop {
            match next_signal(&db_path) {
                Ok(Some(signal)) => {
                    tracing::info!(
                        "[ingest] GMGN cluster: mint={} wallets={} avg_winrate={:.1}% usd={:.2} ts={}",
                        signal.token_mint,
                        signal.wallet_count,
                        signal.avg_winrate * 100.0,
                        signal.amount_usd,
                        signal.signal_timestamp,
                    );
                    if signal.price_usd > 0.0 {
                        tracing::debug!(
                            "[ingest] GMGN USD reference price={:.10}; executor will resolve on-chain SOL fill",
                            signal.price_usd
                        );
                    }

                    let event = SwapEvent {
                        source_wallet: signal.source_wallet,
                        token_mint: signal.token_mint,
                        venue: parse_venue(&signal.venue),
                        direction: SwapDirection::Buy,
                        amount_sol: 0.0,
                        amount_token: 0.0,
                        // GMGN reports USD price. Do not mislabel it as SOL price.
                        // The executor resolves the pool and computes the paper fill.
                        price_sol: 0.0,
                        slot: 0,
                        signature: signal.signal_key,
                    };
                    if tx.send(event).await.is_err() {
                        tracing::error!("[ingest] swap receiver dropped");
                        break;
                    }
                }
                Ok(None) => tokio::time::sleep(std::time::Duration::from_secs(2)).await,
                Err(error) => {
                    // The producer may not have created the table yet. Stay alive and retry.
                    tracing::debug!("[ingest] waiting for GMGN queue: {error}");
                    tokio::time::sleep(std::time::Duration::from_secs(5)).await;
                }
            }
        }
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn maps_known_and_unknown_venues() {
        assert!(matches!(parse_venue("PumpFun"), Venue::PumpFun));
        assert!(matches!(parse_venue("PumpSwap"), Venue::PumpSwap));
        assert!(matches!(parse_venue("other"), Venue::Unknown(_)));
    }
}
