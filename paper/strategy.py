from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable


@dataclass(frozen=True)
class WalletSignal:
    wallet: str
    token: str
    side: str
    winrate: float
    timestamp: int
    price_usd: float
    amount_usd: float
    chain: str = "sol"


@dataclass(frozen=True)
class TokenSignal:
    token: str
    chain: str
    wallets: tuple[WalletSignal, ...]
    strength: float
    latest_timestamp: int
    price_usd: float


def bucket_weight(winrate: float) -> float:
    """User's weighting: 70%+=0.25, 60-70%=0.0625, 50-60%=0.03125."""
    if winrate >= 0.70:
        return 0.25
    if winrate >= 0.60:
        return 0.0625
    if winrate >= 0.50:
        return 0.03125
    return 0.0


def activity_ok(timestamp: int, now: int, max_age_days: int = 7) -> bool:
    return timestamp > now - max_age_days * 86400


def build_signals(
    trades: Iterable[WalletSignal],
    now: int | None = None,
    window_seconds: int = 1800,
    min_strength: float = 1.0,
) -> list[TokenSignal]:
    now = now or int(datetime.now(timezone.utc).timestamp())
    latest: dict[tuple[str, str, str], WalletSignal] = {}
    for trade in trades:
        if trade.side.lower() != "buy" or not trade.token:
            continue
        if not activity_ok(trade.timestamp, now):
            continue
        key = (trade.chain, trade.token, trade.wallet)
        old = latest.get(key)
        if old is None or trade.timestamp > old.timestamp:
            latest[key] = trade

    grouped: dict[tuple[str, str], list[WalletSignal]] = {}
    for trade in latest.values():
        grouped.setdefault((trade.chain, trade.token), []).append(trade)

    result: list[TokenSignal] = []
    for (chain, token), rows in grouped.items():
        newest = max(row.timestamp for row in rows)
        rows = [row for row in rows if newest - row.timestamp <= window_seconds]
        strength = sum(bucket_weight(row.winrate) for row in rows)
        if strength < min_strength:
            continue
        result.append(TokenSignal(
            token=token,
            chain=chain,
            wallets=tuple(sorted(rows, key=lambda row: row.timestamp, reverse=True)),
            strength=strength,
            latest_timestamp=newest,
            price_usd=max((row.price_usd for row in rows), default=0.0),
        ))
    return sorted(result, key=lambda signal: (signal.strength, signal.latest_timestamp), reverse=True)
