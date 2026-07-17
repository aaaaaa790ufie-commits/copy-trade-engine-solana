"""Sentinel — wallet discovery module.

Discovers candidate smart wallets from:
1. DexScreener API (top gainers / trending tokens)
2. Early-buyer reconstruction from token transaction history
3. Manual seed list (if provided)
"""
