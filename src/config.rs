use anyhow::Result;
use serde::Deserialize;
use std::path::Path;

/// Top-level config, mirroring config.toml
#[derive(Clone, Debug, Deserialize)]
pub struct Config {
    pub dry_run: bool,
    pub live: bool,

    pub rpc: RpcConfig,
    pub discovery: DiscoveryConfig,
    pub scoring: ScoringConfig,
    pub simulation: SimulationConfig,
    pub risk: RiskConfig,
    pub executor: ExecutorConfig,
    pub position_manager: PositionManagerConfig,
    pub telemetry: TelemetryConfig,
}

#[derive(Clone, Debug, Deserialize)]
pub struct RpcConfig {
    pub helius_enabled: bool,
    pub alchemy_enabled: bool,
    pub quicknode_enabled: bool,
    pub getblock_enabled: bool,
    pub ankr_enabled: bool,
    pub public_fallback: bool,
    pub priority: RpcPriority,
    pub backoff: RpcBackoff,
}

#[derive(Clone, Debug, Deserialize)]
pub struct RpcPriority {
    pub helius: u32,
    pub alchemy: u32,
    pub quicknode: u32,
    pub getblock: u32,
}

#[derive(Clone, Debug, Deserialize)]
pub struct RpcBackoff {
    pub initial: u64,
    pub max: u64,
    pub multiplier: f64,
}

#[derive(Clone, Debug, Deserialize)]
pub struct DiscoveryConfig {
    pub dex_screener_req_per_min: u32,
    pub seed_wallets_path: String,
    pub early_buyer_max_tokens: usize,
    pub early_buyer_max_wallets: usize,
    pub birdeye_batch_size: usize,
}

#[derive(Clone, Debug, Deserialize)]
pub struct ScoringConfig {
    pub recalc_interval_minutes: u64,
    pub rolling_window_days: u32,
    pub activity_min_tx_per_week: u32,
    pub activity_max_tx_per_week: u32,
    pub recency_decay_multiplier: f64,
    pub cluster_correlation_threshold: f64,
    pub tier_a: TierAConfig,
    pub tier_b: TierBConfig,
}

#[derive(Clone, Debug, Deserialize)]
pub struct TierAConfig {
    pub min_edge_score: f64,
}

#[derive(Clone, Debug, Deserialize)]
pub struct TierBConfig {
    pub min_edge_score: f64,
}

#[derive(Clone, Debug, Deserialize)]
pub struct SimulationConfig {
    pub lag_slots: u64,
    pub jito_tip_per_trade_sol: f64,
    pub network_cost_per_trade_sol: f64,
}

#[derive(Clone, Debug, Deserialize)]
pub struct RiskConfig {
    pub max_concurrent_positions: u32,
    pub max_allocation_pct: f64,
    pub max_per_source_wallet_pct: f64,
    pub stop_loss: StopLossConfig,
    pub security: SecurityConfig,
}

#[derive(Clone, Debug, Deserialize)]
pub struct StopLossConfig {
    pub stop_loss_pct: f64,
    pub trailing_activate_pct: f64,
    pub trailing_distance_pct: f64,
}

#[derive(Clone, Debug, Deserialize)]
pub struct SecurityConfig {
    pub max_top10_holder_pct: f64,
    pub require_lp_burned_or_locked: bool,
    pub require_mint_authority_renounced: bool,
    pub require_freeze_authority_renounced: bool,
}

#[derive(Clone, Debug, Deserialize)]
pub struct ExecutorConfig {
    pub pump_fun_enabled: bool,
    pub pump_swap_enabled: bool,
    pub raydium_enabled: bool,
    pub jupiter_fallback_enabled: bool,
}

#[derive(Clone, Debug, Deserialize)]
pub struct PositionManagerConfig {
    pub check_interval_seconds: u64,
    pub auto_sell_enabled: bool,
}

#[derive(Clone, Debug, Deserialize)]
pub struct TelemetryConfig {
    pub report_time: String,
}

pub fn load(path: impl AsRef<Path>) -> Result<Config> {
    let content = std::fs::read_to_string(path.as_ref())?;
    let cfg: Config = toml::from_str(&content)?;
    Ok(cfg)
}
