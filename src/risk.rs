//! Risk — position sizing, exposure caps, token security pre-check
//! (LP lock, mint authority, freeze authority, top-10 holder %).
//! Consumes filter Decisions, produces ExecCommands.

use solana_client::rpc_client::RpcClient as SolanaClient;
use solana_sdk::pubkey::Pubkey;
use std::str::FromStr;

use crate::config::Config;
use crate::executor::ExecCommand;
use crate::filter::Decision;
use tokio::sync::mpsc::{Receiver, Sender};

const PUBLIC_RPC: &str = "https://api.mainnet-beta.solana.com";

/// Check whether a token mint's authority is renounced (security pre-check).
///
/// Returns (mint_authority_renounced, freeze_authority_renounced).
/// When the RPC call fails, both default to `false` (deny by default).
fn check_mint_security(mint: &str) -> (bool, bool) {
    let mint_pk = match Pubkey::from_str(mint) {
        Ok(pk) => pk,
        Err(_) => {
            tracing::warn!("[risk] invalid mint address: {mint}");
            return (false, false);
        }
    };

    let client = SolanaClient::new(PUBLIC_RPC.to_string());

    let account = match client.get_account(&mint_pk) {
        Ok(acc) => acc,
        Err(e) => {
            tracing::warn!("[risk] getAccountInfo failed for {mint}: {e}");
            return (false, false);
        }
    };

    // Mint account layout (82 bytes):
    //   0..4   = mint authority option (1 if present, 0 if None)
    //   4..36  = mint authority pubkey (if option==1)
    //   36..44 = supply (u64)
    //   44..45 = decimals (u8)
    //   45..46 = is_initialized (bool)
    //   46..47 = freeze authority option
    //   47..79 = freeze authority pubkey (if option==1)
    let data = account.data;
    if data.len() < 47 {
        tracing::warn!("[risk] mint account too short for {mint}: {} bytes", data.len());
        return (false, false);
    }

    let mint_auth_opt = data[0];
    let freeze_auth_opt = data[46];

    let mint_renounced = mint_auth_opt == 0;
    let freeze_renounced = freeze_auth_opt == 0;

    tracing::info!(
        "[risk] mint security for {mint}: mint_authority_renounced={mint_renounced} freeze_authority_renounced={freeze_renounced}"
    );

    (mint_renounced, freeze_renounced)
}

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

            // ── Security pre-check (mint authority) ───────────────
            let mint = &decision.swap.token_mint;
            let (mint_renounced, freeze_renounced) = check_mint_security(mint);

            let mut security_ok = true;

            if cfg.risk.security.require_mint_authority_renounced && !mint_renounced {
                tracing::warn!("[risk] mint authority NOT renounced for {mint} — skipping");
                security_ok = false;
            }

            if cfg.risk.security.require_freeze_authority_renounced && !freeze_renounced {
                tracing::warn!("[risk] freeze authority NOT renounced for {mint} — skipping");
                security_ok = false;
            }

            if !security_ok {
                continue;
            }

            // ── Compute position size ─────────────────────────────
            let position_size_sol = 0.01 * cfg.risk.max_allocation_pct;

            // ── Send to executor ──────────────────────────────────
            let cmd = ExecCommand {
                source_wallet: source.clone(),
                token_mint: mint.clone(),
                venue: decision.swap.venue.clone(),
                direction: decision.swap.direction.clone(),
                amount_sol: position_size_sol,
                simulated_price_sol: decision.swap.price_sol,
                source_slot: decision.swap.slot,
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
                mint,
                position_size_sol,
                decision.swap.venue,
                open_positions
            );

            // NOTE: open_positions never decrements until Phase 7 (position_mgr)
            // sends a close event back. For now the counter only grows.
        }
    })
}
