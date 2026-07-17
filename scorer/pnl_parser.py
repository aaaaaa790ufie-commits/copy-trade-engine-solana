"""
Sentinel — Scorer Module: PnL parsing from raw Solana transactions.

Parses pre/post token balances and SOL balance changes from getTransaction
jsonParsed output to compute realized PnL per trade.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)

LAMPORTS_PER_SOL = 1_000_000_000

# Known DEX program IDs (used for classification only)
DEX_PROGRAMS = {
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "pump_fun",
    "pAMMPxompa13c2qojFgUGSXXysyLLCUmSXwG8M7fKtM": "pump_swap",
    "675kPX9MHTjS2zt1qfr1NYyze2V9cWzmRpJnLkzFY7": "raydium_amm_v4",
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP": "raydium_cpmm",
}


def _account_index(tx: dict[str, Any], wallet_address: str) -> int | None:
    """Return the account index of `wallet_address` in the transaction."""
    for i, acc in enumerate(tx.get("transaction", {}).get("message", {}).get("accountKeys", [])):
        if isinstance(acc, dict):
            if acc.get("pubkey") == wallet_address:
                return i
        elif acc == wallet_address:
            return i
    return None


def _sol_balance_delta(tx: dict[str, Any], acct_idx: int) -> float:
    """SOL balance change for a given account index, in SOL."""
    pre = tx.get("meta", {}).get("preBalances", [])
    post = tx.get("meta", {}).get("postBalances", [])
    if acct_idx < len(pre) and acct_idx < len(post):
        delta_lamports = post[acct_idx] - pre[acct_idx]
        # Negative = wallet spent SOL (buy); Positive = wallet received SOL (sell)
        return delta_lamports / LAMPORTS_PER_SOL
    return 0.0


def _token_deltas(
    tx: dict[str, Any], acct_idx: int
) -> dict[str, float]:
    """Return dict {mint: amount_delta} for the wallet's token accounts.

    Positive delta = wallet received tokens (buy).
    Negative delta = wallet sent tokens (sell).
    """
    meta = tx.get("meta", {})
    pre_tokens = meta.get("preTokenBalances", []) or []
    post_tokens = meta.get("postTokenBalances", []) or []

    pre_by_mint: dict[str, float] = {}
    for entry in pre_tokens:
        if entry.get("accountIndex") != acct_idx:
            continue
        mint = entry.get("mint", "")
        ui = entry.get("uiTokenAmount", {})
        amount = ui.get("uiAmount")
        if amount is not None and mint:
            pre_by_mint[mint] = amount

    post_by_mint: dict[str, float] = {}
    for entry in post_tokens:
        if entry.get("accountIndex") != acct_idx:
            continue
        mint = entry.get("mint", "")
        ui = entry.get("uiTokenAmount", {})
        amount = ui.get("uiAmount")
        if amount is not None and mint:
            post_by_mint[mint] = amount

    deltas: dict[str, float] = {}
    all_mints = set(pre_by_mint.keys()) | set(post_by_mint.keys())
    for mint in all_mints:
        pre_amt = pre_by_mint.get(mint, 0.0)
        post_amt = post_by_mint.get(mint, 0.0)
        delta = post_amt - pre_amt
        if abs(delta) > 1e-12:
            deltas[mint] = delta

    return deltas


def _classify_trade(
    sol_delta: float, token_deltas: dict[str, float]
) -> tuple[str, str, float, float]:
    """Classify a trade as buy/sell/unknown.

    Returns: (direction, token_mint, sol_amount, token_amount)
    """
    # BUY: SOL decreased (spent), at least one token increased (received)
    received_tokens = {m: d for m, d in token_deltas.items() if d > 0}
    sent_tokens = {m: d for m, d in token_deltas.items() if d < 0}

    if sol_delta < 0 and received_tokens:
        # Buy: spent SOL for tokens
        # Pick the token with largest received amount
        best_mint = max(received_tokens, key=received_tokens.get)
        return ("buy", best_mint, abs(sol_delta), received_tokens[best_mint])

    if sol_delta > 0 and sent_tokens:
        # Sell: received SOL for tokens
        best_mint = min(sent_tokens, key=sent_tokens.get)  # most negative = largest amount sent
        return ("sell", best_mint, sol_delta, abs(sent_tokens[best_mint]))

    # Token-for-token swap (no SOL change): treat as sell of sent + buy of received
    if sent_tokens and received_tokens:
        sent_mint = min(sent_tokens, key=sent_tokens.get)
        rcvd_mint = max(received_tokens, key=received_tokens.get)
        logger.debug("Token-for-token swap: %s -> %s", sent_mint[:8], rcvd_mint[:8])
        # This is complex — record as unknown for now with the received direction
        # The PnL will be computed when we eventually sell the received token
        return ("swap", sent_mint, 0.0, abs(sent_tokens[sent_mint]))

    return ("unknown", "", 0.0, 0.0)


# ── In-memory position tracker (per wallet) ────────────────────────


def parse_trades_from_wallet(
    txns: list[dict[str, Any]],
    wallet_address: str,
) -> list[dict[str, Any]]:
    """Parse a list of raw Solana transactions into trade records with PnL.

    Maintains a running position tracker (cost basis per token) so sells
    can compute realized PnL.

    Returns list of trade dicts:
        direction, token_mint, sol_amount, token_amount,
        realized_pnl_sol, block_time, slot, signature, program
    """
    # Sort by slot ascending (not strictly guaranteed in raw list, but best-effort)
    txns_sorted = sorted(txns, key=lambda t: t.get("slot", 0))

    # Position tracking: {mint: {"amount": float, "cost_basis_sol": float}}
    positions: dict[str, dict[str, float]] = {}
    trades: list[dict[str, Any]] = []

    for tx in txns_sorted:
        sig = (
            tx.get("transaction", {}).get("signatures", [None]) or [None]
        )[0]
        slot = tx.get("slot", 0)
        block_time = tx.get("blockTime", 0)
        meta = tx.get("meta", {})
        if meta and meta.get("err"):
            # Skip failed transactions
            continue

        acct_idx = _account_index(tx, wallet_address)
        if acct_idx is None:
            continue

        sol_delta = _sol_balance_delta(tx, acct_idx)
        token_deltas = _token_deltas(tx, acct_idx)

        direction, token_mint, sol_amount, token_amount = _classify_trade(
            sol_delta, token_deltas
        )

        # Detect which DEX program was involved (from top-level instructions)
        program = "unknown"
        instructions = (
            tx.get("transaction", {}).get("message", {}).get("instructions", [])
        )
        for ix in instructions:
            prog_id = ix.get("programId", "") if isinstance(ix, dict) else ""
            if prog_id in DEX_PROGRAMS:
                program = DEX_PROGRAMS[prog_id]
                break
            # Also check inner instructions
            inner_ixs = meta.get("innerInstructions", []) or []
            for inner in inner_ixs:
                for inner_ix in inner.get("instructions", []):
                    inner_prog = inner_ix.get("programId", "")
                    if inner_prog in DEX_PROGRAMS:
                        program = DEX_PROGRAMS[inner_prog]
                        break

        realized_pnl = 0.0

        if direction == "buy" and token_mint:
            # Record position
            if token_amount > 0:
                cost_basis = sol_amount / token_amount
                if token_mint in positions:
                    # Average cost basis
                    old = positions[token_mint]
                    total_tokens = old["amount"] + token_amount
                    total_cost = (old["amount"] * old["cost_basis_sol"]) + sol_amount
                    new_cost = total_cost / total_tokens if total_tokens > 0 else 0
                    positions[token_mint] = {
                        "amount": total_tokens,
                        "cost_basis_sol": new_cost,
                    }
                else:
                    positions[token_mint] = {
                        "amount": token_amount,
                        "cost_basis_sol": cost_basis,
                    }
                logger.debug(
                    "BUY  %s %.6f SOL → %.4f %s (cost=%.8f SOL/token)",
                    wallet_address[:8], sol_amount, token_amount,
                    token_mint[:8], cost_basis,
                )

        elif direction == "sell" and token_mint:
            # Realized PnL
            if token_mint in positions:
                pos = positions[token_mint]
                cost_of_sold = abs(token_amount) * pos["cost_basis_sol"]
                realized_pnl = sol_amount - cost_of_sold
                # Update position
                remaining = pos["amount"] - abs(token_amount)
                if remaining <= 1e-12:
                    del positions[token_mint]
                else:
                    positions[token_mint]["amount"] = remaining
                logger.debug(
                    "SELL %s %.4f %s → %.6f SOL (pnl=%.6f, cost=%.8f/token)",
                    wallet_address[:8], token_amount, token_mint[:8],
                    sol_amount, realized_pnl, pos["cost_basis_sol"],
                )
            else:
                logger.debug(
                    "SELL %s without position — stale or pre-existing holding",
                    wallet_address[:8],
                )

        elif direction == "swap":
            # For token-for-token swaps: treat as sell + buy
            # For now, just note it and skip PnL
            logger.debug(
                "SWAP %s — token-for-token, PnL tracking deferred",
                wallet_address[:8],
            )

        trades.append({
            "direction": direction,
            "token_mint": token_mint,
            "sol_amount": round(sol_amount, 9),
            "token_amount": round(token_amount, 9),
            "realized_pnl_sol": round(realized_pnl, 9),
            "block_time": block_time,
            "slot": slot,
            "signature": sig,
            "program": program,
        })

    return trades
