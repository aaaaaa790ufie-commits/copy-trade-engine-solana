#!/usr/bin/env python3
"""Minimal Telegram Bot API polling client, no private key and no framework."""
from __future__ import annotations
import json, os, sqlite3, time, urllib.parse, urllib.request
DB=os.getenv("SENTINEL_DB","sentinel.db"); TOKEN=os.getenv("TELEGRAM_BOT_TOKEN",""); CHAT=os.getenv("TELEGRAM_CHAT_ID","")
def api(method,data=None):
    url=f"https://api.telegram.org/bot{TOKEN}/{method}"; body=urllib.parse.urlencode(data or {}).encode()
    with urllib.request.urlopen(urllib.request.Request(url,data=body),timeout=30) as r: return json.loads(r.read())
_last_event=0
def push_events(c):
    global _last_event
    rows=c.execute("SELECT id,kind,message FROM engine_events WHERE id>? ORDER BY id",(_last_event,)).fetchall()
    for eid,kind,msg in rows:
        _last_event=eid
        if kind in ("ENTRY","EXIT","WALLET","BANKRUPT"):
            try:
                api("sendMessage",{"chat_id":CHAT,"text":f"{kind}: {msg}"})
                time.sleep(.3)
            except Exception as ex: print("push fail:",ex)
def text(c,command):
    a=c.execute("SELECT budget_sol,initial_budget_sol,bankrupt FROM paper_account WHERE id=1").fetchone()
    if command=="/status":
        p=c.execute("SELECT token_mint,chain,stake_sol,entry_price,peak_price FROM paper_positions WHERE status='open'").fetchall()
        out=[f"Paper trading: {'BANKRUPT' if a[2] else 'LIVE'}",f"Баланс: {a[0]:.5f} SOL / старт {a[1]:.5f}",f"Открытых позиций: {len(p)}"]
        out += [f"{x[1]} {x[0][:8]}... | stake {x[2]:.4f} | entry {x[3]:.8g} | peak {x[4]:.8g}" for x in p]
        return "\n".join(out)
    if command=="/trades":
        rs=c.execute("SELECT chain,token_mint,action,pnl_pct,pnl_sol,reason,event_ts FROM paper_trades ORDER BY id DESC LIMIT 10").fetchall()
        return "\n".join(f"{time.strftime('%Y-%m-%d %H:%M:%S UTC',time.gmtime(r[6]))} {r[2]} {r[0]} {r[1][:8]}... {r[3]*100:+.2f}% {r[4]:+.5f} SOL | {r[5]}" for r in rs) or "Сделок пока нет"
    if command=="/wallets":
        rs=c.execute("SELECT chain,COUNT(*),AVG(winrate) FROM wallet_watch WHERE active=1 GROUP BY chain").fetchall()
        return "\n".join(f"{r[0]}: {r[1]} кошельков, средний winrate {r[2]*100:.1f}%" for r in rs) or "Кошельки ещё не загружены"
    return "/status\n/trades\n/wallets"
def main():
    if not TOKEN: raise SystemExit("TELEGRAM_BOT_TOKEN is required")
    c=sqlite3.connect(DB,check_same_thread=False); offset=0
    while True:
        try:
            for u in api("getUpdates",{"timeout":25,"offset":offset}).get("result",[]):
                offset=u["update_id"]+1; msg=u.get("message",{}); chat=str(msg.get("chat",{}).get("id","")); cmd=(msg.get("text") or "").split()[0]
                if CHAT and chat!=CHAT: continue
                api("sendMessage",{"chat_id":chat,"text":text(c,cmd)})
        except Exception as e: print("telegram:",e); time.sleep(5)
        push_events(c)
if __name__=="__main__": main()
