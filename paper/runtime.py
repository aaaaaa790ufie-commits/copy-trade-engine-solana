from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from .account import PaperAccount
from .gmgn_source import fetch_signals
from .strategy import build_signals

POLL_SECONDS = int(os.getenv('GMGN_POLL_SECONDS', '15'))
COOLDOWN_SECONDS = int(os.getenv('TOKEN_COOLDOWN_SECONDS', '600'))


def _current_price(token: str, chain: str) -> float:
    proc = subprocess.run(['gmgn-cli', 'token', 'info', '--chain', chain, '--address', token, '--raw'], capture_output=True, text=True, timeout=30)
    if proc.returncode:
        return 0.0
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    try:
        value = json.loads(lines[-1])
        while isinstance(value, dict) and 'data' in value:
            value = value['data']
        if isinstance(value, dict):
            return float(value.get('price') or value.get('price_usd') or 0.0)
    except (ValueError, IndexError, json.JSONDecodeError):
        pass
    return 0.0


def run(db_path: str = 'sentinel.db') -> None:
    conn = sqlite3.connect(db_path, timeout=30)
    account = PaperAccount(conn, budget_sol=0.1, entry_sol=0.025)
    last_exit: dict[tuple[str, str], int] = {}
    while True:
        now = int(time.time())
        try:
            signals = build_signals(fetch_signals(), now=now, min_strength=1.0)
            for signal in signals:
                key = (signal.chain, signal.token)
                if key in last_exit and now - last_exit[key] < COOLDOWN_SECONDS:
                    continue
                if account.has_open(signal.token, signal.chain):
                    continue
                opened = account.open_position(signal.token, signal.chain, signal.price_usd, signal.strength, len(signal.wallets))
                if opened:
                    print(f"ENTRY UTC={now} chain={signal.chain} token={signal.token} strength={signal.strength:.5f} wallets={len(signal.wallets)}")
            closed = account.mark_prices(_current_price)
            for token, pnl, reason in closed:
                last_exit[('sol', token)] = now
                print(f"EXIT UTC={now} token={token} pnl={pnl:+.6f} reason={reason}")
            if account.balance() < 0.025:
                print('мы обнулились в papertrading, скажи это своему hermes agent, будем разбираться по сделкам')
                break
        except Exception as exc:
            print(f'paper runtime error: {exc}')
        time.sleep(POLL_SECONDS)
