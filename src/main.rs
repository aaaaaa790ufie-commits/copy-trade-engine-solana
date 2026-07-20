use anyhow::Result;

mod config;
mod ingest;
mod filter;
mod risk;
mod executor;
mod position_mgr;
mod lagfill;

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt::init();
    dotenvy::dotenv().ok();

    let args: Vec<String> = std::env::args().collect();

    // Parse --config <path>
    let config_path = if let Some(pos) = args.iter().position(|a| a == "--config") {
        args.get(pos + 1).cloned().unwrap_or_else(|| "config.toml".into())
    } else {
        "config.toml".into()
    };

    let mut cfg = config::load(&config_path)?;

    // CLI overrides
    if args.contains(&"--dry-run=false".to_string()) {
        cfg.dry_run = false;
    }
    if args.contains(&"--live=true".to_string()) {
        cfg.live = true;
    }

    tracing::info!(
        "Sentinel starting — DRY_RUN={}, LIVE={}, config={}",
        cfg.dry_run, cfg.live, config_path
    );

    if cfg.dry_run {
        tracing::warn!("DRY_RUN mode — no real transactions will be sent");
    }
    if !cfg.live {
        tracing::warn!("LIVE=false — real sendTransaction calls are short-circuited");
    }

    // ── Channel wiring (tokio mpsc) ─────────────────────────────
    let (swap_tx, swap_rx) = tokio::sync::mpsc::channel::<ingest::SwapEvent>(1024);
    let (decision_tx, decision_rx) = tokio::sync::mpsc::channel::<filter::Decision>(256);
    let (exec_tx, exec_rx) = tokio::sync::mpsc::channel::<executor::ExecCommand>(256);

    // Ingest → Filter (SwapEvents)
    let ingest_handle = ingest::spawn(cfg.clone(), swap_tx);
    let filter_handle = filter::spawn(cfg.clone(), swap_rx, decision_tx);

    // Filter → Risk → Executor (Decisions → ExecCommands)
    let exec_tx_risk = exec_tx.clone();
    let risk_handle = risk::spawn(cfg.clone(), decision_rx, exec_tx_risk);
    let executor_handle = executor::spawn(cfg.clone(), exec_rx);

    // Position manager (independent loop checking TP/SL, can trigger sell)
    let pos_mgr_handle = position_mgr::spawn(cfg.clone(), exec_tx.clone());

    // ── Wait for any module to exit ─────────────────────────────
    tokio::select! {
        r = ingest_handle => tracing::error!("ingest exited: {:?}", r),
        r = filter_handle => tracing::error!("filter exited: {:?}", r),
        r = risk_handle => tracing::error!("risk exited: {:?}", r),
        r = executor_handle => tracing::error!("executor exited: {:?}", r),
        r = pos_mgr_handle => tracing::error!("pos_mgr exited: {:?}", r),
    }

    Ok(())
}
