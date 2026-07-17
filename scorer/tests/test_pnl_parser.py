"""Unit tests for PnL parsing."""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scorer.pnl_parser import (
    _account_index,
    _sol_balance_delta,
    _token_deltas,
    _classify_trade,
    parse_trades_from_wallet,
)


def make_tx(
    wallet_pubkey: str,
    sol_delta_lamports: int,
    token_deltas: dict[str, float],
    slot: int = 100,
    block_time: int = 1700000000,
    err: bool = False,
) -> dict:
    """Build a minimal transaction dict for testing."""
    acct_idx = 2  # wallet is at index 2
    
    pre_bal = [1000000000, 2000000000, 1000000000]
    post_bal = pre_bal.copy()
    post_bal[2] = pre_bal[2] + sol_delta_lamports  # Wallet at index 2

    pre_tokens = []
    post_tokens = []
    tok_idx = 0
    for mint, delta in token_deltas.items():
        pre_tokens.append({
            "accountIndex": 2,
            "mint": mint,
            "uiTokenAmount": {"uiAmount": 0.0 if delta > 0 else abs(delta)},
        })
        post_tokens.append({
            "accountIndex": 2,
            "mint": mint,
            "uiTokenAmount": {"uiAmount": delta if delta > 0 else 0.0},
        })
        tok_idx += 1

    tx = {
        "blockTime": block_time,
        "slot": slot,
        "meta": {
            "err": {"Err": "instruction"} if err else None,
            "preBalances": pre_bal,
            "postBalances": post_bal,
            "preTokenBalances": pre_tokens,
            "postTokenBalances": post_tokens,
            "innerInstructions": [],
            "status": {"Ok": None} if not err else {"Err": "instruction"},
        },
        "transaction": {
            "signatures": [f"test_sig_{slot}"],
            "message": {
                "accountKeys": [
                    "fee_payer",
                    "program_id",
                    wallet_pubkey,
                ],
                "instructions": [
                    {"programId": "675kPX9MHTjS2zt1qfr1NYyze2V9cWzmRpJnLkzFY7"}
                ],
            },
        },
        "version": 0,
    }
    return tx


def test_buy():
    """Wallet buys tokens — spends SOL, receives tokens."""
    tx = make_tx(
        "test_wallet", 
        sol_delta_lamports=-100_000_000,  # spent 0.1 SOL
        token_deltas={"So11111111111111111111111111111111111111112": 50.0},  # token mint mock
        slot=1,
    )
    trades = parse_trades_from_wallet([tx], "test_wallet")
    assert len(trades) == 1, f"Expected 1 trade, got {len(trades)}"
    t = trades[0]
    assert t["direction"] == "buy", f"Expected buy, got {t['direction']}"
    assert t["sol_amount"] == 0.1, f"SOL amount should be 0.1, got {t['sol_amount']}"
    assert t["token_amount"] == 50.0, f"Token amount should be 50.0, got {t['token_amount']}"
    assert t["program"] == "raydium_amm_v4", f"Expected raydium, got {t['program']}"
    print(f"  ✓ BUY test passed: {t}")

def test_sell():
    """Wallet sells tokens — receives SOL, spends tokens."""
    # First buy
    buy_tx = make_tx("test_wallet", sol_delta_lamports=-100_000_000,
                      token_deltas={"MY_TOKEN": 50.0}, slot=1)
    # Then sell (half)
    sell_tx = make_tx("test_wallet", sol_delta_lamports=150_000_000,  # received 0.15 SOL
                      token_deltas={"MY_TOKEN": -25.0}, slot=2)
    
    trades = parse_trades_from_wallet([buy_tx, sell_tx], "test_wallet")
    assert len(trades) == 2, f"Expected 2 trades, got {len(trades)}"
    
    t_sell = trades[1]
    assert t_sell["direction"] == "sell"
    # Cost basis: 0.1 SOL / 50 tokens = 0.002 SOL/token
    # Sold 25 tokens at cost of 25 * 0.002 = 0.05 SOL
    # Received 0.15 SOL
    # PnL = 0.15 - 0.05 = 0.10 SOL
    assert abs(t_sell["realized_pnl_sol"] - 0.10) < 0.001, \
        f"PnL should be ~0.10, got {t_sell['realized_pnl_sol']}"
    print(f"  ✓ SELL test passed: pnl={t_sell['realized_pnl_sol']}")

def test_err_tx_skipped():
    """Failed transactions should be skipped."""
    tx = make_tx("test_wallet", sol_delta_lamports=-100_000_000,
                  token_deltas={"TOKEN": 50.0}, slot=1, err=True)
    trades = parse_trades_from_wallet([tx], "test_wallet")
    assert len(trades) == 0, f"Failed tx should be skipped"
    print("  ✓ ERR test passed")

def test_no_trades_empty():
    """No parseable trades → empty result."""
    tx = make_tx("test_wallet", sol_delta_lamports=0,
                  token_deltas={}, slot=1)
    trades = parse_trades_from_wallet([tx], "test_wallet")
    assert len(trades) == 1
    assert trades[0]["direction"] == "unknown"
    print("  ✓ Empty test passed")

if __name__ == "__main__":
    print("Phase 4 — PnL Parser Tests")
    test_buy()
    test_sell()
    test_err_tx_skipped()
    test_no_trades_empty()
    print("\n✅ All tests passed")
