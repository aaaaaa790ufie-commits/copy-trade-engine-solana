from __future__ import annotations

import os
import sqlite3
import time
import requests
from .account import PaperAccount

TOKEN = os.environ['TELEGRAM_BOT_TOKEN']
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
API = f'https://api.telegram.org/bot{TOKEN}'


def send(text: str) -> None:
    requests.post(f'{API}/sendMessage', json={'chat_id': CHAT_ID, 'text': text}, timeout=20)


def format_status(account: PaperAccount) -> str:
    s = account.status()
    return f"PaperTrading\nБаланс: {s['balance_sol']:.6f} SOL\nRealized PnL: {s['realized_pnl_sol']:+.6f} SOL\nОткрытых позиций: {s['open_positions']}\nРазмер входа: {s['entry_sol']:.6f} SOL"


def run(db_path: str = 'sentinel.db') -> None:
    conn = sqlite3.connect(db_path, timeout=30)
    account = PaperAccount(conn)
    offset = 0
    send(format_status(account))
    while True:
        response = requests.get(f'{API}/getUpdates', params={'timeout': 25, 'offset': offset}, timeout=35).json()
        for update in response.get('result', []):
            offset = update['update_id'] + 1
            message = update.get('message', {})
            if CHAT_ID and str(message.get('chat', {}).get('id')) != str(CHAT_ID):
                continue
            command = (message.get('text') or '').split()[0].lower()
            if command in ('/status', '/start'):
                send(format_status(account))
            elif command == '/trades':
                rows = conn.execute("SELECT event_time_utc,side,token,pnl_pct,reason FROM paper_trades ORDER BY id DESC LIMIT 10").fetchall()
                send('\n'.join(f'{r[0]} UTC {r[1].upper()} {r[2][:8]} pnl={r[3]:+.2f}% {r[4]}' for r in rows) or 'Сделок пока нет.')
            elif command == '/wallets':
                rows = conn.execute("SELECT wallet_address,tier,edge_score FROM wallet_scores ORDER BY edge_score DESC LIMIT 20").fetchall()
                send('\n'.join(f'{r[0][:8]}... {r[1]} {r[2]:.2%}' for r in rows) or 'Кошельков пока нет.')
            elif command == '/help':
                send('/status, /trades, /wallets, /help')
        time.sleep(1)
