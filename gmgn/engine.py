#!/usr/bin/env python3
"""GMGN weighted-convergence paper trader. Read-only API, SQLite journal, no real trades."""
from __future__ import annotations
import argparse,json,logging,os,sqlite3,subprocess,time
from collections import defaultdict
from typing import Any
LOG=logging.getLogger('sentinel-engine'); DB=os.getenv('SENTINEL_DB','sentinel.db')
BUDGET=float(os.getenv('PAPER_BUDGET_SOL','0.1')); STAKE=float(os.getenv('PAPER_TRADE_SIZE_SOL','0.025'))
ENTRY_SCORE=float(os.getenv('GMGN_ENTRY_SCORE','1.0')); WINDOW=int(os.getenv('GMGN_CLUSTER_WINDOW_SECONDS','1800')); COOLDOWN=int(os.getenv('GMGN_COOLDOWN_SECONDS','420')); POLL=int(os.getenv('GMGN_POLL_SECONDS','15'))
TRAIL_ACTIVATE=float(os.getenv('TRAILING_ACTIVATE_PCT','25'))/100; TRAIL_DISTANCE=float(os.getenv('TRAILING_DISTANCE_PCT','15'))/100; HARD_STOP=float(os.getenv('HARD_STOP_PCT','45'))/100; FEED_LIMIT=int(os.getenv('GMGN_FEED_LIMIT','200'))
CHAINS=[x.strip() for x in os.getenv('GMGN_CHAINS','sol,robinhood').split(',') if x.strip()]
def cli(args):
 p=subprocess.run(['gmgn-cli',*args,'--raw'],capture_output=True,text=True,timeout=45)
 if p.returncode: raise RuntimeError((p.stderr or p.stdout).strip())
 z=[x.strip() for x in p.stdout.splitlines() if x.strip()]; return json.loads(z[-1]) if z else {}
def unwrap(x):
 while isinstance(x,dict) and 'data' in x and set(x)<= {'code','message','data','success'}: x=x['data']
 return x
def rows(x):
 x=unwrap(x)
 if isinstance(x,list): return [r for r in x if isinstance(r,dict)]
 if isinstance(x,dict):
  for k in ('list','items','result'):
   if isinstance(x.get(k),list): return [r for r in x[k] if isinstance(r,dict)]
  return [x] if x else []
 return []
def num(o,*keys,default=0.0):
 for k in keys:
  v=o
  for part in k.split('.'): v=v.get(part) if isinstance(v,dict) else None
  if v is not None:
   try:
    z=float(v); return z/100 if 'winrate' in k and z>1 else z
   except (TypeError,ValueError): pass
 return default
def maker(t): return str(t.get('maker') or t.get('wallet') or '')
def token(t): return str(t.get('base_address') or t.get('token_address') or '')
def ts(t): return int(num(t,'timestamp','trigger_at'))
def side(t): return str(t.get('side','')).lower()
def winrate(s): return num(s,'winrate','win_rate','pnl_winrate')
def weight(w): return .25 if w>=.70 else .0625 if w>=.60 else .03125 if w>=.50 else 0.0
def allowed(t,chain):
 if chain=='robinhood': return True
 raw=' '.join(str(t.get(k,'')) for k in ('launchpad','launchpad_platform','migrated_pool_exchange')).lower(); bt=t.get('base_token') if isinstance(t.get('base_token'),dict) else {}; raw+=' '+str(bt.get('launchpad','')).lower(); return 'pump' in raw
def price(t):
 p=num(t,'price_now','price_usd','price'); ch=num(t,'price_change'); return p*ch if 'price_now' not in t and ch>0 else p
def init(c):
 c.execute('PRAGMA journal_mode=WAL'); c.executescript("""
 CREATE TABLE IF NOT EXISTS paper_account(id INTEGER PRIMARY KEY CHECK(id=1),budget_sol REAL NOT NULL,initial_budget_sol REAL NOT NULL,bankrupt INTEGER NOT NULL DEFAULT 0,updated_at INTEGER NOT NULL);
 INSERT OR IGNORE INTO paper_account VALUES(1,0.1,0.1,0,strftime('%s','now'));
 CREATE TABLE IF NOT EXISTS paper_positions(token_mint TEXT PRIMARY KEY,chain TEXT NOT NULL,entry_price REAL NOT NULL,peak_price REAL NOT NULL,stake_sol REAL NOT NULL,opened_at INTEGER NOT NULL,signal_score REAL NOT NULL,wallet_count INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'open');
 CREATE TABLE IF NOT EXISTS paper_trades(id INTEGER PRIMARY KEY AUTOINCREMENT,token_mint TEXT NOT NULL,chain TEXT NOT NULL,action TEXT NOT NULL,price REAL NOT NULL,stake_sol REAL NOT NULL,pnl_sol REAL NOT NULL DEFAULT 0,pnl_pct REAL NOT NULL DEFAULT 0,reason TEXT NOT NULL,wallet_count INTEGER NOT NULL DEFAULT 0,signal_score REAL NOT NULL DEFAULT 0,event_ts INTEGER NOT NULL);
 CREATE TABLE IF NOT EXISTS paper_cooldowns(token_mint TEXT NOT NULL,chain TEXT NOT NULL,until_ts INTEGER NOT NULL,PRIMARY KEY(token_mint,chain));
 CREATE TABLE IF NOT EXISTS wallet_watch(address TEXT NOT NULL,chain TEXT NOT NULL,source TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,last_seen INTEGER NOT NULL DEFAULT 0,winrate REAL NOT NULL DEFAULT 0,updated_at INTEGER NOT NULL,PRIMARY KEY(address,chain));
 CREATE TABLE IF NOT EXISTS engine_events(id INTEGER PRIMARY KEY AUTOINCREMENT,event_ts INTEGER NOT NULL,kind TEXT NOT NULL,message TEXT NOT NULL);"""); c.commit()
def event(c,kind,msg): LOG.info('%s: %s',kind,msg); c.execute('INSERT INTO engine_events(event_ts,kind,message) VALUES(?,?,?)',(int(time.time()),kind,msg))
def fetch(chain): return rows(cli(['track','smartmoney','--chain',chain,'--limit',str(FEED_LIMIT)]))
def stats(chain,wallets):
 out={}
 for i in range(0,len(wallets),10):
  b=wallets[i:i+10]
  try: got=rows(cli(['portfolio','stats','--chain',chain,'--wallet',*b,'--period','30d']))
  except Exception as e: LOG.warning('stats %s failed: %s',chain,e); continue
  for r in got:
   a=str(r.get('address') or r.get('wallet') or r.get('wallet_address') or r.get('maker') or '')
   if a: out[a]=r
  if len(b)==1 and b[0] not in out and len(got)==1: out[b[0]]=got[0]
  time.sleep(.2)
 return out
def cooldown(c,m,chain,now):
 r=c.execute('SELECT until_ts FROM paper_cooldowns WHERE token_mint=? AND chain=?',(m,chain)).fetchone(); return bool(r and r[0]>now)
def enter(c,chain,trades,weights,now):
 by=defaultdict(dict)
 for t in trades:
  if not allowed(t,chain) or not token(t) or ts(t)<now-WINDOW: continue
  w=maker(t)
  if w in weights and side(t) in ('buy','sell') and (w not in by[token(t)] or ts(t)>ts(by[token(t)][w])): by[token(t)][w]=t
 openm={r[0] for r in c.execute("SELECT token_mint FROM paper_positions WHERE status='open'")}
 for m,ws in by.items():
  buys={w:t for w,t in ws.items() if side(t)=='buy'}; score=sum(weights[w] for w in buys)
  if score<ENTRY_SCORE or m in openm or cooldown(c,m,chain,now): continue
  p=price(max(buys.values(),key=ts)) if buys else 0
  a=c.execute('SELECT budget_sol,bankrupt FROM paper_account WHERE id=1').fetchone()
  if p<=0 or not a: continue
  if a[1] or a[0]<STAKE:
   if not a[1]: c.execute('UPDATE paper_account SET bankrupt=1,updated_at=? WHERE id=1',(now,)); event(c,'BANKRUPT','обнулились в papertrading, скажи это своему hermes agent, будем разбираться по сделкам')
   continue
  c.execute('UPDATE paper_account SET budget_sol=budget_sol-?,updated_at=? WHERE id=1',(STAKE,now)); c.execute('INSERT INTO paper_positions VALUES(?,?,?,?,?,?,?,?,'"'"'open'"'"')',(m,chain,p,p,STAKE,now,score,len(buys)))
  c.execute('INSERT INTO paper_trades(token_mint,chain,action,price,stake_sol,reason,wallet_count,signal_score,event_ts) VALUES(?,?,?,?,?,?,?,?,?)',(m,chain,'ENTRY',p,STAKE,'weighted cluster',len(buys),score,now)); event(c,'ENTRY',f'{chain} {m} | wallets={len(buys)} score={score:.4f} | {STAKE:.4f} SOL')
def mark(c,chain,trades,now):
 latest={}
 for t in trades:
  if token(t) and allowed(t,chain) and price(t)>0 and (token(t) not in latest or ts(t)>ts(latest[token(t)])): latest[token(t)]=t
 q=c.execute('SELECT token_mint,entry_price,peak_price,stake_sol,opened_at,signal_score,wallet_count FROM paper_positions WHERE chain=? AND status='"'"'open'"'"'',(chain,)).fetchall()
 for m,e,peak,stake,opened,score,wc in q:
  t=latest.get(m)
  if not t: continue
  cur=price(t); peak=max(peak,cur); ret=cur/e-1; c.execute('UPDATE paper_positions SET peak_price=? WHERE token_mint=?',(peak,m)); hard=ret<=-HARD_STOP; trail=peak/e-1>=TRAIL_ACTIVATE and cur<=peak*(1-TRAIL_DISTANCE)
  if hard or trail:
   reason='hard stop -45%' if hard else 'trailing stop 15%'; pnl=stake*ret; c.execute('UPDATE paper_account SET budget_sol=budget_sol+?,updated_at=? WHERE id=1',(stake+pnl,now)); c.execute("UPDATE paper_positions SET status='closed' WHERE token_mint=?",(m,)); c.execute('INSERT INTO paper_cooldowns VALUES(?,?,?) ON CONFLICT(token_mint,chain) DO UPDATE SET until_ts=excluded.until_ts',(m,chain,now+COOLDOWN)); c.execute('INSERT INTO paper_trades(token_mint,chain,action,price,stake_sol,pnl_sol,pnl_pct,reason,wallet_count,signal_score,event_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(m,chain,'EXIT',cur,stake,pnl,ret,reason,wc,score,now)); event(c,'EXIT',f'{chain} {m} | {ret*100:.2f}% ({pnl:+.5f} SOL) | {reason}')
def cycle(c):
 now=int(time.time())
 for chain in CHAINS:
  try: tr=fetch(chain)
  except Exception as e: LOG.warning('feed %s failed: %s',chain,e); continue
  makers=sorted({maker(t) for t in tr if maker(t)}); st=stats(chain,makers); good={w:weight(winrate(s)) for w,s in st.items() if winrate(s)>=.50 and num(s,'buy_count','buy_count_7d','trades_7d')>0}
  for w,wt in good.items(): c.execute("INSERT INTO wallet_watch(address,chain,source,last_seen,winrate,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(address,chain) DO UPDATE SET last_seen=excluded.last_seen,winrate=excluded.winrate,updated_at=excluded.updated_at",(w,chain,'gmgn',now,winrate(st[w]),now))
  enter(c,chain,tr,good,now); mark(c,chain,tr,now)
 c.commit()
def main():
 ap=argparse.ArgumentParser(); ap.add_argument('--once',action='store_true'); ap.add_argument('--db-path',default=DB); a=ap.parse_args(); logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s'); c=sqlite3.connect(a.db_path,timeout=30); init(c)
 try:
  while True: cycle(c); 
  
 finally: c.close()
if __name__=='__main__': main()
