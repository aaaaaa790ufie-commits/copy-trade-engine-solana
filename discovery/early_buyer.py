"""Early-buyer wallet reconstruction.

For a given token, fetches the earliest transaction signatures via
getSignaturesForAddress and attempts to identify the first N buyer wallets.
"""

import logging
import time
from typing import Any
import requests

logger = logging.getLogger(__name__)

# Fallback public RPC (no key needed, but 2-5s stale and rate-limited)
PUBLIC_RPC_URL = "https://api.mainnet-beta.solana.com"

# Known DEX program IDs on Solana (used to filter for swap instructions)
PUMP_FUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_SWAP_PROGRAM = "pAMMPxompa13c2qojFgUGSXXysyLLCUmSXwG8M7fKtM"
RAYDIUM_AMM_V4 = "675kPX9MHTjS2zt1qfr1NYyze2V9cWzmRpJnLkzFY7"
RAYDIUM_CPMM = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP"

SWAP_PROGRAMS = {
    PUMP_FUN_PROGRAM: "pump_fun",
    PUMP_SWAP_PROGRAM: "pump_swap",
    RAYDIUM_AMM_V4: "raydium_amm_v4",
    RAYDIUM_CPMM: "raydium_cpmm",
}

class RpcClient:
    """Minimal Solana RPC client for read-only operations."""

    def __init__(self, endpoint: str | None = None):
        self.endpoint = endpoint or PUBLIC_RPC_URL
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
        })
        self._req_count = 0

    def _call(self, method: str, params: list | None = None) -> dict[str, Any] | None:
        payload = {
            "jsonrpc": "2.0",
            "id": self._req_count,
            "method": method,
            "params": params or [],
        }
        self._req_count += 1
        try:
            resp = self.session.post(self.endpoint, json=payload, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            if "error" in data:
                logger.warning("RPC error [%s]: %s", method, data["error"])
                return None
            return data
        except requests.exceptions.RequestException as e:
            logger.warning("RPC call failed [%s]: %s", method, e)
            return None

    def get_signatures_for_address(
        self, address: str, limit: int = 100, before: str | None = None
    ) -> list[dict[str, Any]]:
        """Fetch signatures for a given address."""
        params = [address, {"limit": limit}]
        if before:
            params[1]["before"] = before
        result = self._call("getSignaturesForAddress", params)
        if result and "result" in result:
            return result["result"]
        return []

    def get_transaction(self, signature: str) -> dict[str, Any] | None:
        """Fetch a single transaction by signature."""
        params = [
            signature,
            {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0},
        ]
        result = self._call("getTransaction", params)
        if result and "result" in result:
            return result["result"]
        return None

    def get_token_accounts_by_owner(
        self, owner: str, mint: str
    ) -> list[dict[str, Any]]:
        """Get token accounts for a specific mint owned by an address."""
        params = [
            owner,
            {"mint": mint},
            {"encoding": "jsonParsed"},
        ]
        result = self._call("getTokenAccountsByOwner", params)
        if result and "result" in result:
            return result["result"].get("value", [])
        return []


def find_early_buyers(
    token_mint: str,
    max_wallets: int = 20,
    rpc: RpcClient | None = None,
) -> list[dict[str, Any]]:
    """Identify early buyer wallets for a token.

    Strategy: fetch the earliest signatures for the token's mint address,
    then for each signature, fetch the transaction and identify the buyer
    wallet from the instruction accounts.

    Returns list of dicts with keys: address, first_seen_slot, signature.
    """
    if rpc is None:
        rpc = RpcClient()

    # 1. Fetch earliest signatures for the token mint
    signatures = []
    before = None
    for _ in range(5):  # up to 500 sigs (5 pages of 100)
        page = rpc.get_signatures_for_address(token_mint, limit=100, before=before)
        if not page:
            break
        signatures.extend(page)
        if len(page) < 100:
            break
        before = page[-1]["signature"]
        time.sleep(0.2)  # rate limit courtesy

    if not signatures:
        logger.info("No signatures found for token %s", token_mint)
        return []

    # 2. Take the earliest N signatures and try to extract buyer wallets
    earliest = signatures[:max_wallets * 2]  # fetch extra to account for failures
    buyers: list[dict[str, Any]] = []
    seen_wallets: set[str] = set()

    for sig_info in earliest:
        if len(buyers) >= max_wallets:
            break

        sig = sig_info["signature"]
        tx = rpc.get_transaction(sig)
        if not tx:
            continue

        # Try to extract the buyer from transaction accounts
        buyer = _extract_buyer_from_tx(tx, token_mint)
        if buyer and buyer not in seen_wallets:
            seen_wallets.add(buyer)
            buyers.append({
                "address": buyer,
                "first_seen_slot": sig_info.get("slot", 0),
                "signature": sig,
            })
            logger.debug("Found early buyer: %s (sig: %s)", buyer, sig[:16])

    logger.info(
        "Found %d unique early buyers for token %s",
        len(buyers), token_mint
    )
    return buyers


def _extract_buyer_from_tx(tx: dict[str, Any], token_mint: str) -> str | None:
    """Try to extract the buyer wallet address from a parsed transaction.

    This is a heuristic: look at account keys and instruction accounts
    to find the fee payer / first signer, which is typically the buyer.
    """
    try:
        tx_meta = tx.get("meta") or {}
        tx_msg = tx.get("transaction", {}).get("message", {})

        # Fee payer is almost always the transaction submitter (the buyer)
        account_keys = tx_msg.get("accountKeys", [])
        if not account_keys:
            return None

        # The first signer is typically the fee payer
        for acct in account_keys:
            if isinstance(acct, dict) and acct.get("signer"):
                return acct.get("pubkey")
            # Flat string array fallback
            if isinstance(acct, str):
                return acct

        return None
    except (KeyError, IndexError, TypeError) as e:
        logger.debug("Failed to extract buyer from tx: %s", e)
        return None


def cross_reference_wallets(
    token_buyers: dict[str, list[dict[str, Any]]],
    min_tokens: int = 2,
) -> list[dict[str, Any]]:
    """Cross-reference wallets that appear as early buyers across multiple tokens.

    Args:
        token_buyers: dict of token_mint -> list of buyer dicts
        min_tokens: minimum number of tokens a wallet must appear in

    Returns:
        List of wallet dicts with: address, token_count, tokens, avg_first_seen_slot
    """
    wallet_tokens: dict[str, set[str]] = {}
    wallet_slots: dict[str, list[int]] = {}

    for token_mint, buyers in token_buyers.items():
        for b in buyers:
            addr = b["address"]
            if addr not in wallet_tokens:
                wallet_tokens[addr] = set()
                wallet_slots[addr] = []
            wallet_tokens[addr].add(token_mint)
            wallet_slots[addr].append(b.get("first_seen_slot", 0))

    results = []
    for addr, tokens in wallet_tokens.items():
        if len(tokens) >= min_tokens:
            results.append({
                "address": addr,
                "token_count": len(tokens),
                "tokens": list(tokens),
                "avg_first_seen_slot": (
                    sum(wallet_slots[addr]) / len(wallet_slots[addr])
                    if wallet_slots[addr] else 0
                ),
            })

    results.sort(key=lambda x: x["token_count"], reverse=True)
    return results
