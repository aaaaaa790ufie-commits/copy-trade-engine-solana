from __future__ import annotations

import json
import os
import subprocess
from typing import Any
from .strategy import WalletSignal

FEED_LIMIT = int(os.getenv('GMGN_FEED_LIMIT', '200'))


def _cli(args: list[str]) -> Any:
    proc = subprocess.run(['gmgn-cli', *args, '--raw'], capture_output=True, text=True, timeout=45)
    if proc.returncode:
        raise RuntimeError((proc.stderr or proc.stdout).strip())
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    return json.loads(lines[-1])


def _unwrap(value: Any) -> Any:
    while isinstance(value, dict) and 'data' in value and len(value) <= 4:
        value = value['data']
    return value


def _rows(value: Any) -> list[dict[str, Any]]:
    value = _unwrap(value)
    if isinstance(value, list):
        return [x for x in value if isinstance(x, dict)]
    if isinstance(value, dict) and isinstance(value.get('list'), list):
        return [x for x in value['list'] if isinstance(x, dict)]
    return []


def _num(row: dict, *keys: str, default: float = 0.0) -> float:
    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        try:
            parsed = float(value)
            return parsed / 100.0 if 'winrate' in key and parsed > 1 else parsed
        except (TypeError, ValueError):
            pass
    return default


def _wallet_stats(wallets: list[str]) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for start in range(0, len(wallets), 10):
        batch = wallets[start:start + 10]
        payload = _cli(['portfolio', 'stats', '--chain', 'sol', '--wallet', *batch, '--period', '30d'])
        rows = _rows(payload)
        for row in rows:
            address = str(row.get('wallet_address') or row.get('address') or row.get('wallet') or '')
            if address:
                result[address] = row
        if len(batch) == 1 and len(rows) == 1 and batch[0] not in result:
            result[batch[0]] = rows[0]
    return result


def fetch_signals() -> list[WalletSignal]:
    rows = _rows(_cli(['track', 'smartmoney', '--chain', 'sol', '--limit', str(FEED_LIMIT)]))
    wallets = sorted({str(row.get('maker')) for row in rows if row.get('maker')})
    stats = _wallet_stats(wallets)
    signals: list[WalletSignal] = []
    for row in rows:
        wallet = str(row.get('maker') or '')
        token = str(row.get('base_address') or '')
        stat = stats.get(wallet, {})
        winrate = _num(stat, 'winrate', 'win_rate')
        if winrate < 0.50 or not wallet or not token:
            continue
        timestamp = int(_num(row, 'timestamp'))
        signals.append(WalletSignal(wallet, token, str(row.get('side', '')), winrate, timestamp, _num(row, 'price_usd'), _num(row, 'amount_usd'), 'sol'))
    return signals
