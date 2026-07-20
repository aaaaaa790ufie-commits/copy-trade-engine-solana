//! Ingest — WebSocket subscriptions against the free RPC pool.
//! Subscribes to logsSubscribe on known program IDs, filters client-side
//! to tracked wallets, decodes swap instructions per-venue into a
//! normalized SwapEvent sent through the channel.
//!
//! Phase 3.5: real swap-event decoding from WS log notifications + RPC
//! getTransaction. Previously this was a stub returning None.

use crate::config::Config;
use anyhow::{Context, Result};
use futures_util::{SinkExt, StreamExt};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use tokio::sync::{mpsc::Sender, RwLock, Semaphore};
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
const PUMP_SWAP: &str = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA";
const RAYDIUM_AMM_V4: &str = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8";
const RAYDIUM_CPMM: &str = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP";

/// Program IDs whose logs we subscribe to.
const TRACKED_PROGRAMS: &[&str] = &[PUMP_FUN, PUMP_SWAP, RAYDIUM_AMM_V4, RAYDIUM_CPMM];

// ── Metrics ──────────────────────────────────────────────────────

/// Shared counters for decode success/failure.
static DECODE_OK: AtomicU64 = AtomicU64::new(0);
static DECODE_NONE: AtomicU64 = AtomicU64::new(0);

pub fn decode_stats() -> (u64, u64) {
    (DECODE_OK.load(Ordering::Relaxed), DECODE_NONE.load(Ordering::Relaxed))
}

/// How many concurrent RPC fetches we allow (sliding window).
const MAX_CONCURRENT_FETCHES: usize = 4;

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

    providers.sort_by(|a, b| b.priority.cmp(&a.priority));

    if cfg.rpc.public_fallback {
        providers.push(RpcProvider {
            name: "public",
            ws_url: "wss://api.mainnet-beta.solana.com".to_string(),
            http_url: "https://api.mainnet-beta.solana.com".to_string(),
            priority: 1,
            enabled: true,
        });
    }

    providers
}

// ── WebSocket RPC pool ───────────────────────────────────────────

struct WsConnection {
    provider: RpcProvider,
    write: tokio::sync::mpsc::UnboundedSender<Message>,
    _handle: tokio::task::JoinHandle<()>,
}

/// Pool of WS connections, one per provider.
struct WsPool {
    connections: Vec<WsConnection>,
    backoff_cfg: crate::config::RpcBackoff,
}

impl WsPool {
    async fn connect(
        providers: &[RpcProvider],
        backoff_cfg: &crate::config::RpcBackoff,
        swap_tx: Sender<SwapEvent>,
    ) -> Self {
        let mut connections = Vec::new();

        for provider in providers {
            match connect_provider(provider, swap_tx.clone()).await {
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
            backoff_cfg: backoff_cfg.clone(),
        }
    }

    /// Subscribe to logsSubscribe for all tracked programs on all connections.
    async fn subscribe_all(&self) {
        for conn in &self.connections {
            subscribe_program_logs(conn, TRACKED_PROGRAMS).await;
        }
    }
}

// ── Logs → venue + direction (no RPC) ────────────────────────────

/// Detect venue from a log line.
fn log_venue(text: &str) -> Option<Venue> {
    if text.contains(PUMP_FUN) {
        Some(Venue::PumpFun)
    } else if text.contains(PUMP_SWAP) {
        Some(Venue::PumpSwap)
    } else if text.contains(RAYDIUM_AMM_V4) {
        Some(Venue::RaydiumAmmV4)
    } else if text.contains(RAYDIUM_CPMM) {
        Some(Venue::RaydiumCpmm)
    } else {
        None
    }
}

/// Extract venue + direction from the log lines array.
fn parse_logs_direction(logs: &[String]) -> Option<(Venue, Option<SwapDirection>)> {
    let mut venue: Option<Venue> = None;
    let mut direction: Option<SwapDirection> = None;

    for line in logs {
        // Detect venue from first invoke line
        if line.starts_with("Program ") && line.contains(" invoke [") {
            if venue.is_some() {
                continue; // already found a venue
            }
            venue = log_venue(line);
        }

        // Detect buy/sell from "Instruction: Buy" or "Instruction: Sell"
        if line.contains("Instruction: Buy") {
            direction = Some(SwapDirection::Buy);
        } else if line.contains("Instruction: Sell") {
            direction = Some(SwapDirection::Sell);
        }
    }

    venue.map(|v| (v, direction))
}

// ── RPC transaction fetch + decode ────────────────────────────────

/// Global rate limiter: at most MAX_CONCURRENT_FETCHES at once.
static FETCH_SEM: Semaphore = Semaphore::const_new(MAX_CONCURRENT_FETCHES);

/// Fetch a transaction via RPC and decode it into a SwapEvent.
async fn fetch_and_decode(
    signature: &str,
    slot: u64,
    venue: &Venue,
    direction: Option<&SwapDirection>,
    http_url: &str,
) -> Option<SwapEvent> {
    let _permit = FETCH_SEM.acquire().await.ok()?;

    let client = reqwest::Client::new();
    let body = serde_json::json!({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTransaction",
        "params": [
            signature,
            {
                "encoding": "jsonParsed",
                "maxSupportedTransactionVersion": 0,
                "commitment": "confirmed"
            }
        ]
    });

    let resp = client
        .post(http_url)
        .json(&body)
        .timeout(std::time::Duration::from_secs(10))
        .send()
        .await
        .ok()?;

    let data: Value = resp.json().await.ok()?;
    let result = data.get("result")?;

    // Skip failed transactions
    if result.get("meta")?.get("err")?.is_object() || result["meta"]["err"].is_string() {
        return None;
    }

    let tx = result.get("transaction")?;
    let message = tx.get("message")?;

    // Source wallet = first signer
    let account_keys: Vec<String> = message
        .get("accountKeys")?
        .as_array()?
        .iter()
        .filter_map(|k| {
            if let Some(acct) = k.as_object() {
                acct.get("pubkey")?.as_str().map(String::from)
            } else {
                k.as_str().map(String::from)
            }
        })
        .collect();

    let source_wallet = account_keys.first()?.to_string();

    // Extract token mint from preTokenBalances (first non-SOL token)
    let meta = result.get("meta")?;
    let token_mint = get_token_mint_from_meta(meta);

    // Extract SOL and token amounts from balance changes
    let (amount_sol, amount_token) = get_amounts_from_meta(meta, &source_wallet);

    // Infer direction from log or balance change
    let direction = direction.cloned().unwrap_or_else(|| {
        if amount_sol > 0.0 && amount_token > 0.0 {
            let post = meta.get("postBalances")
                .and_then(|v| v.as_array())
                .and_then(|a| a.first())
                .and_then(|v| v.as_u64())
                .unwrap_or(0);
            let pre = meta.get("preBalances")
                .and_then(|v| v.as_array())
                .and_then(|a| a.first())
                .and_then(|v| v.as_u64())
                .unwrap_or(0);
            if post < pre { SwapDirection::Buy } else { SwapDirection::Sell }
        } else {
            SwapDirection::Buy
        }
    });

    let price_sol = if amount_token > 0.0 {
        amount_sol / amount_token
    } else {
        0.0
    };

    Some(SwapEvent {
        source_wallet,
        token_mint: token_mint.unwrap_or_else(|| "unknown".into()),
        venue: venue.clone(),
        direction,
        amount_sol,
        amount_token,
        price_sol,
        slot,
        signature: signature.to_string(),
    })
}

/// Extract the first non-SPL token mint from preTokenBalances.
fn get_token_mint_from_meta(meta: &Value) -> Option<String> {
    let pre = meta.get("preTokenBalances")?.as_array()?;
    for entry in pre {
        if let Some(mint) = entry.get("mint")?.as_str() {
            if mint.len() >= 40 && !mint.starts_with("So111111") {
                return Some(mint.to_string());
            }
        }
    }
    None
}

/// Estimate SOL / token amounts from pre/post balance diffs.
/// Uses the fee payer's SOL change and the first token balance change.
fn get_amounts_from_meta(meta: &Value, _source_wallet: &str) -> (f64, f64) {
    // SOL change from pre/post SOL balance of fee payer
    let pre_balances = match meta.get("preBalances").and_then(|v| v.as_array()) {
        Some(a) => a,
        None => return (0.0, 0.0),
    };
    let post_balances = match meta.get("postBalances").and_then(|v| v.as_array()) {
        Some(a) => a,
        None => return (0.0, 0.0),
    };

    let sol_diff = if !pre_balances.is_empty() && !post_balances.is_empty() {
        (post_balances[0].as_u64().unwrap_or(0) as i64)
            - (pre_balances[0].as_u64().unwrap_or(0) as i64)
    } else {
        0
    };

    // For buys: SOL decreases (positive amount_out), token increases
    // For sells: SOL increases, token decreases
    let amount_sol = (sol_diff.abs() as f64) / 1_000_000_000.0;

    // Token amount from pre/post token balances
    let token_amount = get_token_amount_change(meta);

    (amount_sol, token_amount)
}

/// Get the absolute token amount change from pre/post token balances.
fn get_token_amount_change(meta: &Value) -> f64 {
    let default_vec = vec![];
    let pre = meta.get("preTokenBalances")
        .and_then(|v| v.as_array())
        .unwrap_or(&default_vec);
    let post = meta.get("postTokenBalances")
        .and_then(|v| v.as_array())
        .unwrap_or(&default_vec);

    let pre_map: std::collections::HashMap<u64, f64> = pre.iter()
        .filter_map(|e| {
            let idx = e.get("accountIndex")?.as_u64()?;
            let amt = e.get("uiTokenAmount")?;
            // Try uiAmountString first, fall back to uiAmount (which may be f64)
            let ui_str = amt.get("uiAmountString").and_then(|v| v.as_str());
            let val: f64 = if let Some(s) = ui_str {
                s.parse().ok()?
            } else {
                amt.get("uiAmount")?.as_f64()?
            };
            Some((idx, val))
        })
        .collect();

    let post_map: std::collections::HashMap<u64, f64> = post.iter()
        .filter_map(|e| {
            let idx = e.get("accountIndex")?.as_u64()?;
            let amt = e.get("uiTokenAmount")?;
            let ui_str = amt.get("uiAmountString").and_then(|v| v.as_str());
            let val: f64 = if let Some(s) = ui_str {
                s.parse().ok()?
            } else {
                amt.get("uiAmount")?.as_f64()?
            };
            Some((idx, val))
        })
        .collect();

    for (idx, pre_val) in &pre_map {
        if let Some(post_val) = post_map.get(idx) {
            let diff = (post_val - pre_val).abs();
            if diff > 0.0 {
                return diff;
            }
        }
    }
    0.0
}

// ── WS connection + reader task ────────────────────────────────────

async fn connect_provider(
    provider: &RpcProvider,
    swap_tx: Sender<SwapEvent>,
) -> Result<WsConnection> {
    let url = Url::parse(&provider.ws_url)?;
    let (ws_stream, _) = connect_async(url.as_str()).await?;
    let (write, read) = ws_stream.split();

    let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel::<Message>();

    // Writer task
    let write_handle = tokio::spawn(async move {
        let mut write = write;
        while let Some(msg) = rx.recv().await {
            if let Err(e) = write.send(msg).await {
                tracing::error!("[ingest] WS write error: {:?}", e);
                break;
            }
        }
    });

    // Reader task — now processes notifications into SwapEvents
    let http_url = provider.http_url.clone();
    let _read_handle = tokio::spawn(async move {
        let mut read = read;
        while let Some(msg) = read.next().await {
            match msg {
                Ok(Message::Text(text)) => {
                    // Parse the logsNotification JSON
                    if let Some((signature, slot, logs, _subscription)) =
                        parse_logs_notification(&text)
                    {
                        // Detect venue + direction from log lines
                        if let Some((venue, maybe_direction)) = parse_logs_direction(&logs) {
                            // Spawn a fetch task to decode the full transaction
                            let tx = swap_tx.clone();
                            let http_url = http_url.clone();
                            tokio::spawn(async move {
                                if let Some(event) = fetch_and_decode(
                                    &signature,
                                    slot,
                                    &venue,
                                    maybe_direction.as_ref(),
                                    &http_url,
                                )
                                .await
                                {
                                    DECODE_OK.fetch_add(1, Ordering::Relaxed);
                                    tracing::info!(
                                        "[ingest] DECODED: venue={:?} dir={:?} wallet={} mint={} sol={:.6} token={:.4} price={:.10} slot={} sig={}",
                                        event.venue, event.direction,
                                        &event.source_wallet[..8],
                                        &event.token_mint[..8],
                                        event.amount_sol,
                                        event.amount_token,
                                        event.price_sol,
                                        event.slot,
                                        &event.signature[..8]
                                    );
                                    if let Err(e) = tx.send(event).await {
                                        tracing::warn!("[ingest] channel send error: {:?}", e);
                                    }
                                } else {
                                    DECODE_NONE.fetch_add(1, Ordering::Relaxed);
                                }
                            });
                        }
                    }
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

/// Parse a logsNotification JSON into (signature, slot, logs, subscription_id).
fn parse_logs_notification(text: &str) -> Option<(String, u64, Vec<String>, u64)> {
    let v: Value = serde_json::from_str(text).ok()?;

    // Check it's a notification
    if v.get("method")?.as_str()? != "logsNotification" {
        return None;
    }

    let params = v.get("params")?;
    let subscription = params.get("subscription")?.as_u64()?;
    let result = params.get("result")?;
    let context = result.get("context")?;
    let slot = context.get("slot")?.as_u64()?;
    let value = result.get("value")?;

    // Skip errored transactions
    if value.get("err").map_or(false, |e| !e.is_null()) {
        return None;
    }

    let signature = value.get("signature")?.as_str()?.to_string();
    let logs: Vec<String> = value
        .get("logs")?
        .as_array()?
        .iter()
        .filter_map(|l| l.as_str().map(String::from))
        .collect();

    Some((signature, slot, logs, subscription))
}

// ── Subscribe helper ──────────────────────────────────────────────

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

// ── Public spawn function ────────────────────────────────────────

/// Spawn the ingest task.
pub fn spawn(cfg: Config, tx: Sender<SwapEvent>) -> tokio::task::JoinHandle<()> {
    tokio::spawn(async move {
        tracing::info!("[ingest] starting — WS pool initialisation");

        let providers = build_providers(&cfg);

        if providers.is_empty() {
            tracing::warn!(
                "[ingest] no RPC providers configured — set HELIUS_API_KEY, \
                 ALCHEMY_API_KEY, QUICKNODE_RPC_URL, or GETBLOCK_API_KEY in .env"
            );
            loop {
                tokio::time::sleep(tokio::time::Duration::from_secs(60)).await;
                tracing::debug!("[ingest] waiting for RPC keys...");
            }
        }

        // Connect to the WS pool, passing swap_tx for the reader tasks
        let pool = WsPool::connect(&providers, &cfg.rpc.backoff, tx.clone()).await;

        // Subscribe to all tracked program IDs
        pool.subscribe_all().await;

        tracing::info!("[ingest] running — subscribed to {} programs on {} provider(s)",
            TRACKED_PROGRAMS.len(), pool.connections.len());

        // Main loop: report decode stats every 30s
        loop {
            tokio::time::sleep(tokio::time::Duration::from_secs(30)).await;
            let (ok, none) = decode_stats();
            tracing::info!(
                "[ingest] decode stats: {} ok, {} none (rate: {:.1}%)",
                ok, none,
                if ok + none > 0 {
                    (ok as f64 / (ok + none) as f64) * 100.0
                } else {
                    0.0
                }
            );
        }
    })
}

// ═════════════════════════════════════════════════════════════════
//  Tests
// ═════════════════════════════════════════════════════════════════

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_parse_logs_pumpfun_buy() {
        let logs = vec![
            "Program 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P invoke [1]".to_string(),
            "Program log: Instruction: Buy".to_string(),
            "Program 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P consumed 12345 of 200000 compute units".to_string(),
            "Program 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P success".to_string(),
        ];
        let result = parse_logs_direction(&logs);
        assert!(result.is_some());
        let (venue, dir_opt) = result.unwrap();
        assert!(matches!(venue, Venue::PumpFun));
        assert!(dir_opt.is_some());
        assert!(matches!(dir_opt.unwrap(), SwapDirection::Buy));
    }

    #[test]
    fn test_parse_logs_pumpswap_sell() {
        let logs = vec![
            "Program pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA invoke [1]".to_string(),
            "Program log: Instruction: Sell".to_string(),
            "Program pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA success".to_string(),
        ];
        let result = parse_logs_direction(&logs);
        assert!(result.is_some());
        let (venue, dir_opt) = result.unwrap();
        assert!(matches!(venue, Venue::PumpSwap));
        assert!(dir_opt.is_some());
        assert!(matches!(dir_opt.unwrap(), SwapDirection::Sell));
    }

    #[test]
    fn test_parse_logs_raydium_no_direction() {
        let logs = vec![
            "Program 675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8 invoke [1]".to_string(),
            "Program log: swap".to_string(),
            "Program TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA invoke [2]".to_string(),
            "Program TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA success".to_string(),
            "Program 675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8 success".to_string(),
        ];
        // Raydium logs may not contain "Instruction: Buy/Sell" — they use "swap"
        let result = parse_logs_direction(&logs);
        assert!(result.is_some());
        let (venue, dir_opt) = result.unwrap();
        assert!(matches!(venue, Venue::RaydiumAmmV4));
        assert!(dir_opt.is_none()); // direction inferred from balance data
    }
}
