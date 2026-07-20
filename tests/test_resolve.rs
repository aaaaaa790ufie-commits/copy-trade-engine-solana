use solana_client::rpc_client::RpcClient;
use solana_client::rpc_config::RpcProgramAccountsConfig;
use solana_client::rpc_filter::{RpcFilterType, Memcmp};
use solana_client::rpc_config::RpcAccountInfoConfig;
use solana_sdk::commitment_config::CommitmentConfig;
use solana_sdk::pubkey::Pubkey;
use std::str::FromStr;

#[test]
fn test_resolve() {
    dotenvy::dotenv().ok();
    let key = std::env::var("HELIUS_API_KEY").unwrap_or_default();
    let url = if !key.is_empty() {
        format!("https://mainnet.helius-rpc.com/?api-key={}", key)
    } else {
        "https://api.mainnet-beta.solana.com".to_string()
    };
    
    println!("Using RPC URL: {}", url);
    let client = RpcClient::new(url);
    let program_id = Pubkey::from_str("pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA").unwrap();
    let mint_pubkey = Pubkey::from_str("DezXAZ8z7PnrFcPybzaJmr1Wps231WY4DP85r75pXXdb").unwrap();
    
    let filters = vec![
        RpcFilterType::Memcmp(Memcmp::new_raw_bytes(0, vec![241, 154, 109, 4, 17, 177, 109, 188])),
        RpcFilterType::Memcmp(Memcmp::new_raw_bytes(43, mint_pubkey.to_bytes().to_vec())),
    ];
    
    let config = RpcProgramAccountsConfig {
        filters: Some(filters),
        account_config: RpcAccountInfoConfig {
            encoding: None,
            data_slice: None,
            commitment: Some(CommitmentConfig::processed()),
            min_context_slot: None,
        },
        with_context: None,
        sort_results: None,
    };
    
    let accounts = client.get_program_accounts_with_config(&program_id, config);
    println!("Found accounts: {:?}", accounts);
}
