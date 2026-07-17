"""DexScreener API client — no API key required.

Rate limits: 300 req/min on pairs endpoints, 60 req/min on token profiles.
"""

import time
import logging
from typing import Any
import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://api.dexscreener.com"

class DexScreenerClient:
    """Thin client for DexScreener's free API."""

    def __init__(self, req_per_min: int = 60):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Sentinel/0.1 (wallet-discovery)",
        })
        self.min_interval = 60.0 / req_per_min
        self._last_call = 0.0

    def _rate_limit(self) -> None:
        elapsed = time.time() - self._last_call
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_call = time.time()

    def _get(self, path: str, params: dict | None = None) -> Any:
        self._rate_limit()
        url = f"{BASE_URL}{path}"
        try:
            resp = self.session.get(url, params=params, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            logger.warning("DexScreener request failed: %s — %s", url, e)
            return None

    def get_latest_token_profiles(self, limit: int = 50) -> list[dict[str, Any]]:
        """Fetch latest token profiles (newly created tokens).

        Returns dicts with keys: tokenAddress, chainId, url, icon, links, etc.
        No trading data (price/volume) — use get_token_pairs for that.
        """
        data = self._get("/token-profiles/latest/v1")
        if not data or not isinstance(data, list):
            return []
        solana = [t for t in data if t.get("chainId") == "solana"]
        return solana[:limit]

    def get_token_pairs(self, token_address: str) -> list[dict[str, Any]]:
        """Fetch all pairs for a specific token address.

        Returns pairs with full trading data:
          baseToken, quoteToken, priceNative, priceUsd, txns,
          volume, priceChange, liquidity, fdv, pairCreatedAt
        """
        data = self._get(f"/tokens/v1/{token_address}")
        if not data or not isinstance(data, list):
            return []
        return data

    def search_pairs(self, query: str) -> list[dict[str, Any]]:
        """Search pairs by symbol, name, or address.

        Returns pairs sorted by liquidity/volume (DexScreener default).
        """
        data = self._get("/latest/dex/search", params={"q": query})
        if not data or not isinstance(data, dict):
            return []
        return data.get("pairs", [])

    def get_trending_solana_pairs(self, limit: int = 30) -> list[dict[str, Any]]:
        """Get trending Solana pairs by searching for popular bases.

        Uses search queries to find high-volume Solana pairs.
        """
        # Search for common Solana DEX pairs — returns sorted by relevance/volume
        data = self._get("/latest/dex/search", params={"q": "solana"})
        if not data or not isinstance(data, dict):
            return []

        pairs = data.get("pairs", [])
        solana_pairs = [
            p for p in pairs
            if p.get("chainId") == "solana"
            and p.get("liquidity", {}).get("usd", 0)
        ]
        return solana_pairs[:limit]

    def get_top_gainers(self, limit: int = 15) -> list[dict[str, Any]]:
        """Fetch top gainers among Solana pairs.

        Strategy: search for Solana pairs, sort by priceChange.h24 descending.
        """
        pairs = self.get_trending_solana_pairs(limit=50)
        gainers = sorted(
            pairs,
            key=lambda p: float(p.get("priceChange", {}).get("h24", 0) or 0),
            reverse=True,
        )
        return gainers[:limit]
