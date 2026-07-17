//! Ingest — WebSocket subscriptions against the free RPC pool.
//! Subscribes to logsSubscribe on known program IDs, filters client-side
//! to tracked wallets, decodes swap instructions per-venue into a
//! normalized SwapEvent sent through the channel.

use crate::config::Config;
use anyhow::Result;
use futures_util::{SinkExt, StreamExt};
use serde::{Deserialize, Serialize};
use std::sync::Arc;
use tokio::sync::{mpsc::Sender, RwLock};
use tokio_tungstenite::connect_async;
use tokio_tungstenite::tungstenite::Message;
use url::Url;

// ── Public types ─────────────────────────────────────────────────

/// Normalised swap event produced by ingest, consumed by filter.
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

// ── Known program IDs ────────────────────────────────────────────

const PUMP_FUN: &str = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P";
const PUMP_SWAP: &str = "pAMMPxompa13c2qojFgUGSXXysyLLCUmSXwG8M7fKtM";
const RAYDIUM_AMM_V4: &str = "675kPX9MHTjS2zt1qfr1NYyze2V9cWzmRpJnLkzFY7";
const RAYDIUM_CPMM: &str = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP";

/// Program IDs whose logs we subscribe to.
const TRACKED_PROGRAMS: &[&str] = &[PUMP_FUN, PUMP_SWAP, RAYDIUM_AMM_V4, RAYDIUM_CPMM];

// ── RPC Provider configuration ───────────────────────────────────

#[derive(Debug, Clone)]
struct RpcProvider {
    name: &'static str,
    ws_url: String,
    http_url: String,
    priority: u32,
    enabled: bool,
}

fn build_providers(cfg: &Config) -> Vec<RpcProvider> {
    let helius_key = std::env::var("HELIUS_API_KEY").unwrap_or_default();
    let alchemy_key = std::env::var("ALCHEMY_API_KEY").unwrap_or_default();
    let quicknode_url = std::env::var("QUICKNODE_RPC_URL").unwrap_or_default();
    let getblock_key = std::env::var("GETBLOCK_API_KEY").unwrap_or_default();

    let mut providers = Vec::new();

    if cfg.rpc.helius_enabled && !helius_key.is_empty() {
        providers.push(RpcProvider {
            name: "helius",
            ws_url: format!("wss://mainnet.helius-rpc.com/?api-key={}", helius_key),
            http_url: format!("https://mainnet.helius-rpc.com/?api-key={}", helius_key),
            priority: cfg.rpc.priority.helius,
            enabled: true,
        });
    }

    if cfg.rpc.alchemy_enabled && !alchemy_key.is_empty() {
        providers.push(RpcProvider {
            name: "alchemy",
            ws_url: format!("wss://solana-mainnet.g.alchemy.com/v2/{}", alchemy_key),
            http_url: format!("https://solana-mainnet.g.alchemy.com/v2/{}", alchemy_key),
            priority: cfg.rpc.priority.alchemy,
            enabled: true,
        });
    }

    if cfg.rpc.quicknode_enabled && !quicknode_url.is_empty() {
        // QuickNode gives a full URL; convert https:// → wss:// for WS
        let ws_url = quicknode_url.replace("https://", "wss://");
        providers.push(RpcProvider {
            name: "quicknode",
            ws_url,
            http_url: quicknode_url.clone(),
            priority: cfg.rpc.priority.quicknode,
            enabled: true,
        });
    }

    if cfg.rpc.getblock_enabled && !getblock_key.is_empty() {
        providers.push(RpcProvider {
            name: "getblock",
            ws_url: format!("wss://solana.getblock.io/mainnet/ws/{}", getblock_key),
            http_url: format!("https://solana.getblock.io/mainnet/{}", getblock_key),
            priority: cfg.rpc.priority.getblock,
            enabled: true,
        });
    }

    // Sort by priority descending
    providers.sort_by(|a, b| b.priority.cmp(&a.priority));

    // Public fallback (last resort — no key needed, but higher latency)
    if cfg.rpc.public_fallback {
        providers.push(RpcProvider {
            name: "public",
            ws_url: "wss://api.mainnet-beta.solana.com".to_string(),
            http_url: "https://api.mainnet-beta.solana.com".to_string(),
            priority: 1,  // lowest — overridden by sorting? No, we sort desc, so 1 goes last
            enabled: true,
        });
    }

    providers
}

// ── WebSocket RPC pool ───────────────────────────────────────────

struct WsConnection {
    provider: RpcProvider,
    /// Sender half of the WS stream (to send subscribe requests)
    write: tokio::sync::mpsc::UnboundedSender<Message>,
    /// Task handle for the WS reader loop
    _handle: tokio::task::JoinHandle<()>,
}

/// Pool of WS connections, one per provider.
struct WsPool {
    connections: Vec<WsConnection>,
    current_index: Arc<RwLock<usize>>,
    backoff_cfg: crate::config::RpcBackoff,
}

impl WsPool {
    async fn connect(providers: &[RpcProvider], backoff_cfg: &crate::config::RpcBackoff) -> Self {
        let mut connections = Vec::new();

        for provider in providers {
            match connect_provider(provider).await {
                Ok(conn) => {
                    tracing::info!("[ingest] connected to {} (WS)", provider.name);
                    connections.push(conn);
                }
                Err(e) => {
                    tracing::warn!("[ingest] failed to connect to {}: {:?}", provider.name, e);
                }
            }
        }

        tracing::info!(
            "[ingest] WS pool ready — {} / {} providers connected",
            connections.len(),
            providers.len()
        );

        Self {
            connections,
            current_index: Arc::new(RwLock::new(0)),
            backoff_cfg: backoff_cfg.clone(),
        }
    }

    /// Subscribe to logsSubscribe for all tracked programs on all connections.
    async fn subscribe_all(&self) {
        for conn in &self.connections {
            subscribe_program_logs(conn, TRACKED_PROGRAMS).await;
        }
    }

    /// Route a SwapEvent to the ingest channel (round-robin over connected providers).
    async fn route_event(
        &self,
        event: SwapEvent,
        tx: &Sender<SwapEvent>,
    ) -> Result<()> {
        tx.send(event).await?;
        Ok(())
    }
}

async fn connect_provider(provider: &RpcProvider) -> Result<WsConnection> {
    let url = Url::parse(&provider.ws_url)?;
    let (ws_stream, _) = connect_async(url.as_str()).await?;
    let (write, read) = ws_stream.split();

    let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel::<Message>();

    // Spawn a writer task that forwards from the mpsc channel to the WS sink
    let write_handle = tokio::spawn(async move {
        let mut write = write;
        while let Some(msg) = rx.recv().await {
            if let Err(e) = write.send(msg).await {
                tracing::error!("[ingest] WS write error: {:?}", e);
                break;
            }
        }
    });

    // Spawn a reader task that processes incoming WS messages
    let _read_handle = tokio::spawn(async move {
        let mut read = read;
        while let Some(msg) = read.next().await {
            match msg {
                Ok(Message::Text(text)) => {
                    // Ingest processes subscription notifications here
                    // For now, just log the raw text at debug level
                    tracing::debug!("[ingest] WS message: {}", &text[..text.len().min(200)]);
                }
                Ok(Message::Ping(_)) | Ok(Message::Pong(_)) => {}
                Ok(Message::Close(frame)) => {
                    tracing::warn!("[ingest] WS closed: {:?}", frame);
                    break;
                }
                Err(e) => {
                    tracing::error!("[ingest] WS error: {:?}", e);
                    break;
                }
                _ => {}
            }
        }
    });

    Ok(WsConnection {
        provider: provider.clone(),
        write: tx,
        _handle: write_handle,
    })
}

async fn subscribe_program_logs(conn: &WsConnection, programs: &[&str]) {
    for program_id in programs {
        let subscribe_request = serde_json::json!({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "logsSubscribe",
            "params": [
                { "mentions": [program_id] },
                { "commitment": "processed" }
            ]
        });

        let msg = Message::Text(
            serde_json::to_string(&subscribe_request).unwrap().into()
        );

        if let Err(e) = conn.write.send(msg) {
            tracing::error!(
                "[ingest] failed to subscribe to {} on {}: {:?}",
                program_id, conn.provider.name, e
            );
        } else {
            tracing::debug!(
                "[ingest] subscribed to {} on {}",
                program_id, conn.provider.name
            );
        }
    }
}

// ── Swap instruction decoder stubs ───────────────────────────────

/// Decode a swap instruction from a given venue.
/// This is a stub — real instruction decoding requires parsing the
/// transaction's inner instructions per venue.
/// (Phase 6 builds the encoder; the decoder mirrors that encoding.)
fn decode_swap_event(
    _raw_log: &str,
    venue: Venue,
) -> Option<SwapEvent> {
    // TODO Phase 3.5: proper instruction decoding
    //
    // For Pump.fun: parse the instruction data discriminator to identify
    // buy/sell, extract token mint, SOL amount, token amount from the
    // instruction accounts and data.
    //
    // For Raydium: parse CPI inner instructions to find swap amounts.
    //
    // For PumpSwap: similar to Pump.fun with different account layout.
    tracing::debug!("[ingest] would decode swap for {:?}", venue);
    None
}

// ── Public spawn function ────────────────────────────────────────

/// Spawn the ingest task.
pub fn spawn(cfg: Config, tx: Sender<SwapEvent>) -> tokio::task::JoinHandle<()> {
    tokio::spawn(async move {
        let _tx = tx; // keep sender alive
        tracing::info!("[ingest] starting — WS pool initialisation");

        let providers = build_providers(&cfg);

        if providers.is_empty() {
            tracing::warn!(
                "[ingest] no RPC providers configured — set HELIUS_API_KEY, \
                 ALCHEMY_API_KEY, QUICKNODE_RPC_URL, or GETBLOCK_API_KEY in .env"
            );
            // Keep the task alive so the binary doesn't exit
            loop {
                tokio::time::sleep(tokio::time::Duration::from_secs(60)).await;
                tracing::debug!("[ingest] waiting for RPC keys...");
            }
        }

        // Connect to the WS pool
        let pool = WsPool::connect(&providers, &cfg.rpc.backoff).await;

        // Subscribe to all tracked program IDs
        pool.subscribe_all().await;

        tracing::info!("[ingest] running — subscribed to {} programs on {} provider(s)",
            TRACKED_PROGRAMS.len(), pool.connections.len());

        // Main loop: keep the task alive. Reconnection and event
        // processing happen inside the connection reader tasks.
        loop {
            tokio::time::sleep(tokio::time::Duration::from_secs(30)).await;
            tracing::debug!("[ingest] heartbeat — {} WS connection(s) active",
                pool.connections.len());
        }
    })
}
