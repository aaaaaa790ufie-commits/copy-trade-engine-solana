#!/usr/bin/env python3
from __future__ import annotations
import json, logging, os, shutil, sqlite3, subprocess, time
from collections import defaultdict
LOG=logging.getLogger("paper-engine")
DB=os.getenv("SENTINEL_DB","sentinel.db"); BUDGET=float(os.getenv("PAPER_BUDGET_SOL","0.1")); STAKE=float(os.getenv("PAPER_TRADE_SIZE_SOL","0.025")); WINDOW=int(os.getenv("GMGN_CLUSTER_WINDOW_SECONDS","1800")); COOLDOWN=int(os.getenv("GMGN_COOLDOWN_SECONDS","420")); POLL=int(os.getenv("GMGN_POLL_SECONDS","15")); ENTRY=float(os.getenv("GMGN_ENTRY_SCORE","1.0")); TRAIL_ACT=float(os.getenv("TRAILING_ACTIVATE_PCT","25"))/100; TRAIL_DIST=float(os.getenv("TRAILING_DISTANCE_PCT","15"))/100; HARD=float(os.getenv("HARD_STOP_PCT","45"))/100; LIMIT=int(os.getenv("GMGN_FEED_LIMIT","200")); CHAINS=[x.strip() for x in os.getenv("GMGN_CHAINS","sol,robinhood").split(",") if x.strip()]
MAX_HOLD=int(os.getenv("GMGN_MAX_HOLD_SECONDS","21600")); ZERO_TTL=int(os.getenv("GMGN_ZERO_WINRATE_TTL_SECONDS","3600")); PRICE_TTL=int(os.getenv("GMGN_PRICE_TTL_SECONDS","60"))
def _find_gmgn():
    for name in ("gmgn-cli","gmgn-cli.cmd"):
        found=shutil.which(name)
        if found: return found
    return "gmgn-cli.cmd" if os.name=="nt" else "gmgn-cli"
_GMGN=_find_gmgn()
def cli(args):
 p=subprocess.run([_GMGN,*args,"--raw"],capture_output=True,text=True,timeout=45)
 if p.returncode: raise RuntimeError((p.stderr or p.stdout).strip())
 lines=[x.strip() for x in p.stdout.splitlines() if x.strip()]; return json.loads(lines[-1]) if lines else {}
def list_rows(x):
 if isinstance(x,dict) and isinstance(x.get("data"),dict): x=x["data"]
 if isinstance(x,dict) and isinstance(x.get("list"),list): x=x["list"]
 return x if isinstance(x,list) else ([x] if isinstance(x,dict) and x else [])
def n(o,*keys):
 for k in keys:
  v=o
  for part in k.split("."): v=v.get(part) if isinstance(v,dict) else None
  if v is not None:
   try: return float(v)/100 if "winrate" in k and float(v)>1 else float(v)
   except (TypeError,ValueError): pass
 return 0.0
def wallet(t): return str(t.get("maker") or t.get("wallet") or "")
def mint(t): return str(t.get("base_address") or t.get("token_address") or "")
def stamp(t): return int(n(t,"timestamp","trigger_at"))
def quote(t): return str(t.get("side","")).lower()
def wr(s): return n(s,"winrate","win_rate","pnl_stat.winrate")
def weight(x): return .25 if x>=.70 else .0625 if x>=.60 else .03125 if x>=.50 else 0.0
def px(t): return n(t,"price_now","price_usd","price")
_price_cache={}
def token_price(chain,mint):
 """Independent mark price from `token info` (cached PRICE_TTL sec); 0.0 when unavailable."""
 now=time.time(); hit=_price_cache.get((chain,mint))
 if hit and now-hit[0]<PRICE_TTL: return hit[1]
 p=0.0
 try:
  row=(list_rows(cli(["token","info","--chain",chain,"--address",mint])) or [{}])[0]; p=n(row,"price","price_usd","usd_price","price_now")
 except Exception as e: LOG.warning("price %s %s: %s",chain,mint[:8],e)
 _price_cache[(chain,mint)]=(now,p); return p
def allowed(t,chain):
 if chain=="robinhood": return True
 raw=" ".join(str(t.get(k,"")) for k in ("launchpad","launchpad_platform","migrated_pool_exchange")); b=t.get("base_token") if isinstance(t.get("base_token"),dict) else {}; return "pump" in (raw+" "+str(b.get("launchpad",""))).lower()
def init(c):
 c.execute("PRAGMA journal_mode=WAL")
 c.executescript("""
CREATE TABLE IF NOT EXISTS paper_account(id INTEGER PRIMARY KEY CHECK(id=1),budget_sol REAL NOT NULL,initial_budget_sol REAL NOT NULL,bankrupt INTEGER NOT NULL DEFAULT 0,updated_at INTEGER NOT NULL);
INSERT OR IGNORE INTO paper_account VALUES(1,0.1,0.1,0,strftime('%s','now'));
CREATE TABLE IF NOT EXISTS paper_positions(token_mint TEXT PRIMARY KEY,chain TEXT NOT NULL,entry_price REAL NOT NULL,peak_price REAL NOT NULL,stake_sol REAL NOT NULL,opened_at INTEGER NOT NULL,signal_score REAL NOT NULL,wallet_count INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'open');
CREATE TABLE IF NOT EXISTS paper_trades(id INTEGER PRIMARY KEY AUTOINCREMENT,token_mint TEXT NOT NULL,chain TEXT NOT NULL,action TEXT NOT NULL,price REAL NOT NULL,stake_sol REAL NOT NULL,pnl_sol REAL NOT NULL DEFAULT 0,pnl_pct REAL NOT NULL DEFAULT 0,reason TEXT NOT NULL,wallet_count INTEGER NOT NULL,signal_score REAL NOT NULL,event_ts INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS paper_cooldowns(token_mint TEXT NOT NULL,chain TEXT NOT NULL,until_ts INTEGER NOT NULL,PRIMARY KEY(token_mint,chain));
CREATE TABLE IF NOT EXISTS wallet_watch(address TEXT NOT NULL,chain TEXT NOT NULL,source TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,last_seen INTEGER NOT NULL DEFAULT 0,winrate REAL NOT NULL DEFAULT 0,updated_at INTEGER NOT NULL,PRIMARY KEY(address,chain));
CREATE TABLE IF NOT EXISTS wallet_blacklist(address TEXT NOT NULL,chain TEXT NOT NULL,blacklisted_at INTEGER NOT NULL,reason TEXT,PRIMARY KEY(address,chain));
CREATE TABLE IF NOT EXISTS engine_events(id INTEGER PRIMARY KEY AUTOINCREMENT,event_ts INTEGER NOT NULL,kind TEXT NOT NULL,message TEXT NOT NULL);
"""); c.commit()
def emit(c,kind,msg): LOG.info("%s: %s",kind,msg); c.execute("INSERT INTO engine_events VALUES(NULL,?,?,?)",(int(time.time()),kind,msg))
def is_blacklisted(c,addr,chain): return bool(c.execute("SELECT 1 FROM wallet_blacklist WHERE address=? AND chain=?",(addr,chain)).fetchone())
def get_stats(chain,wallets):
 out={}
 for i in range(0,len(wallets),10):
  b=wallets[i:i+10]
  try: got=list_rows(cli(["portfolio","stats","--chain",chain,"--wallet",*b,"--period","30d"]))
  except Exception as e: LOG.warning("stats %s: %s",chain,e); continue
  for r in got:
   a=str(r.get("address") or r.get("wallet") or r.get("wallet_address") or r.get("maker") or "")
   if a: out[a]=r
  if len(b)==1 and b[0] not in out and len(got)==1: out[b[0]]=got[0]
  time.sleep(0.25)
 return out
STATS_REFRESH_SEC=int(os.getenv("GMGN_STATS_TTL_SECONDS","3600"))
def refresh_wallet_stats(c,chain,now):
 stale=c.execute("SELECT address FROM wallet_watch WHERE chain=? AND (winrate=0 OR ?-updated_at>=?) LIMIT 200",(chain,now,STATS_REFRESH_SEC)).fetchall()
 if not stale: return 0
 addrs=[r[0] for r in stale]; st=get_stats(chain,addrs); upd=0; new_high=[]
 for w,data in st.items():
  wrv=wr(data); buys=n(data,"buy","buy_count","sell","sell_count","trades_7d")
  if wrv>0 and buys>0:
   old=c.execute("SELECT winrate FROM wallet_watch WHERE address=? AND chain=?",(w,chain)).fetchone()
   ls=int(n(data,"last_timestamp"))
   if ls>0: c.execute("UPDATE wallet_watch SET winrate=?,last_seen=?,updated_at=? WHERE address=? AND chain=?",(wrv,ls,now,w,chain))
   else: c.execute("UPDATE wallet_watch SET winrate=?,updated_at=? WHERE address=? AND chain=?",(wrv,now,w,chain))
   upd+=1
   if wrv>=.70 and old and old[0]<.70: new_high.append((w[:8],wrv))
  elif wrv>0 and buys==0:
   c.execute("UPDATE wallet_watch SET winrate=?,updated_at=? WHERE address=? AND chain=?",(wrv,now,w,chain))
 if upd:
  LOG.info("refreshed stats for %d/%d stale wallets on %s",upd,len(addrs),chain)
  if new_high: emit(c,"WALLET",f"{chain} | NEW high-winrate: {len(new_high)} wallet(s) >=70%, ex: {new_high[0][0]}... {new_high[0][1]*100:.0f}%")
 return upd
_DISCOV_CYCLE=0
def discover_wallets(c,chain,now):
 global _DISCOV_CYCLE; _DISCOV_CYCLE+=1; addrs=set()
 try:
  d=cli(["track","kol","--chain",chain,"--limit","100"])
  for t in (d.get("list") or (d if isinstance(d,list) else [])):
   w=wallet(t)
   if w: addrs.add(w)
 except Exception as e: LOG.warning("kol %s: %s",chain,e)
 if _DISCOV_CYCLE%4==0:
  try:
   d=cli(["market","trending","--chain",chain,"--interval","1h","--raw"])
   items=(d.get("data",{}).get("rank") or d.get("list") or [])[:3]
   for item in items:
    ta=item.get("address","")
    if not ta: continue
    try:
     tr=cli(["token","traders","--chain",chain,"--address",ta,"--limit","50","--order-by","profit"])
     for t in (tr.get("data",{}).get("list") or tr.get("list") or []):
      addr=t.get("wallet_address","") or t.get("address","")
      if addr: addrs.add(addr)
    except Exception as ex: LOG.warning("traders %s: %s",ta[:8],ex)
  except Exception as e: LOG.warning("trending %s: %s",chain,e)
 if addrs:
  for w in addrs:
   if not is_blacklisted(c,w,chain): c.execute("INSERT OR IGNORE INTO wallet_watch(address,chain,source,last_seen,winrate,updated_at) VALUES(?,?,?,?,?,?)",(w,chain,"gmgn",now,0,now))
  LOG.info("discovered +%d wallets from KOL/traders on %s",len(addrs),chain)
def cleanup_wallets(c,chain,now):
 """Blacklist only wallets with a CONFIRMED sub-50% winrate. Zero-winrate rows (stats never
 fetched yet) are dropped after ZERO_TTL without blacklisting, so a transient API failure or
 rate limit can never blacklist a good wallet forever. Manual seeds are never auto-dropped."""
 low=c.execute("SELECT address FROM wallet_watch WHERE chain=? AND winrate>0 AND winrate<0.50",(chain,)).fetchall()
 if low:
  c.executemany("INSERT OR IGNORE INTO wallet_blacklist(address,chain,blacklisted_at,reason) VALUES(?,?,?,'low_winrate')",[(r[0],chain,now) for r in low])
  c.execute("DELETE FROM wallet_watch WHERE chain=? AND winrate>0 AND winrate<0.50",(chain,))
 c.execute("DELETE FROM wallet_watch WHERE chain=? AND winrate=0 AND source!='manual_seed' AND ?-updated_at>=?",(chain,now,ZERO_TTL))
def cooling(c,m,chain,now):
 r=c.execute("SELECT until_ts FROM paper_cooldowns WHERE token_mint=? AND chain=?",(m,chain)).fetchone(); return bool(r and r[0]>now)
def enter(c,chain,trades,weights,now):
 latest=defaultdict(dict)
 for t in trades:
  if allowed(t,chain) and mint(t) and stamp(t)>=now-WINDOW and wallet(t) in weights and quote(t) in ("buy","sell"):
   if wallet(t) not in latest[mint(t)] or stamp(t)>stamp(latest[mint(t)][wallet(t)]): latest[mint(t)][wallet(t)]=t
 open_mints={r[0] for r in c.execute("SELECT token_mint FROM paper_positions WHERE status='open'")}
 for m,ws in latest.items():
  buys={w:t for w,t in ws.items() if quote(t)=="buy"}; score=sum(weights[w] for w in buys)
  if score<ENTRY or m in open_mints or cooling(c,m,chain,now): continue
  p=token_price(chain,m) or (px(max(buys.values(),key=stamp)) if buys else 0); a=c.execute("SELECT budget_sol,bankrupt FROM paper_account WHERE id=1").fetchone()
  if p<=0 or not a: continue
  if a[0]<STAKE:
   fully_invested=bool(c.execute("SELECT 1 FROM paper_positions WHERE status='open' LIMIT 1").fetchone())
   if not a[1] and not fully_invested: c.execute("UPDATE paper_account SET bankrupt=1,updated_at=? WHERE id=1",(now,)); emit(c,"BANKRUPT","обнулились в papertrading, скажи это своему hermes agent, будем разбираться по сделкам")
   continue
  c.execute("UPDATE paper_account SET budget_sol=budget_sol-?,updated_at=? WHERE id=1",(STAKE,now)); c.execute("INSERT INTO paper_positions(token_mint,chain,entry_price,peak_price,stake_sol,opened_at,signal_score,wallet_count,status) VALUES(?,?,?,?,?,?,?,?,?)",(m,chain,p,p,STAKE,now,score,len(buys),"open")); c.execute("INSERT INTO paper_trades(token_mint,chain,action,price,stake_sol,reason,wallet_count,signal_score,event_ts) VALUES(?,?,?,?,?,?,?,?,?)",(m,chain,"ENTRY",p,STAKE,"weighted cluster",len(buys),score,now)); emit(c,"ENTRY",f"{chain} {m} | wallets={len(buys)} score={score:.4f} | {STAKE:.4f} SOL")
def exits(c,chain,trades,now):
 latest={}
 for t in trades:
  if allowed(t,chain) and mint(t) and px(t)>0 and (mint(t) not in latest or stamp(t)>stamp(latest[mint(t)])): latest[mint(t)]=t
 positions=c.execute("SELECT token_mint,entry_price,peak_price,stake_sol,signal_score,wallet_count,opened_at FROM paper_positions WHERE chain=? AND status=?",(chain,"open")).fetchall()
 for m,entry,peak,stake,score,count,opened in positions:
  current=token_price(chain,m)
  if current<=0 and m in latest: current=px(latest[m])
  expired=now-opened>=MAX_HOLD
  if current<=0:
   if expired: LOG.warning("position %s past max hold but no price available; retrying next cycle",m[:8])
   continue
  peak=max(peak,current); change=current/entry-1; c.execute("UPDATE paper_positions SET peak_price=? WHERE token_mint=?",(peak,m)); hard=change<=-HARD; trailing=(peak/entry-1)>=TRAIL_ACT and current<=peak*(1-TRAIL_DIST)
  if hard or trailing or expired:
   reason=f"hard stop -{HARD*100:.0f}%" if hard else (f"trailing stop {TRAIL_DIST*100:.0f}%" if trailing else f"max hold {MAX_HOLD//3600}h"); pnl=stake*change; c.execute("UPDATE paper_account SET budget_sol=budget_sol+?,updated_at=? WHERE id=1",(stake+pnl,now)); c.execute("UPDATE paper_positions SET status='closed' WHERE token_mint=?",(m,)); c.execute("INSERT INTO paper_cooldowns VALUES(?,?,?) ON CONFLICT(token_mint,chain) DO UPDATE SET until_ts=excluded.until_ts",(m,chain,now+COOLDOWN)); c.execute("INSERT INTO paper_trades(token_mint,chain,action,price,stake_sol,pnl_sol,pnl_pct,reason,wallet_count,signal_score,event_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?)",(m,chain,"EXIT",current,stake,pnl,change,reason,count,score,now)); emit(c,"EXIT",f"{chain} {m} | {change*100:.2f}% ({pnl:+.5f} SOL) | {reason}")
 a=c.execute("SELECT budget_sol,bankrupt FROM paper_account WHERE id=1").fetchone()
 if a and a[1] and a[0]>=STAKE:
  c.execute("UPDATE paper_account SET bankrupt=0,updated_at=? WHERE id=1",(now,)); emit(c,"RECOVERY",f"баланс {a[0]:.5f} SOL снова покрывает ставку {STAKE:.4f} — paper-трейдинг возобновлён")
def cycle(c):
 now=int(time.time())
 for chain in CHAINS:
  try: trades=list_rows(cli(["track","smartmoney","--chain",chain,"--limit",str(LIMIT)]))
  except Exception as e: LOG.warning("feed %s: %s",chain,e); continue
  makers=sorted({wallet(t) for t in trades if wallet(t)}); stats=get_stats(chain,makers); weights={w:weight(wr(s)) for w,s in stats.items() if wr(s)>=.50 and n(s,"buy","buy_count","buy_count_7d","trades_7d")>0}
  before=c.execute("SELECT COUNT(*) FROM wallet_watch WHERE chain=? AND active=1",(chain,)).fetchone()[0]; new_w=0; high_wr=[]
  for w,z in weights.items():
   exist=c.execute("SELECT 1 FROM wallet_watch WHERE address=? AND chain=?",(w,chain)).fetchone()
   if not exist: new_w+=1
   wrv=wr(stats[w])
   if wrv>=.70: high_wr.append((w[:8],wrv))
   if wrv>0 and not is_blacklisted(c,w,chain):
    c.execute("INSERT INTO wallet_watch(address,chain,source,last_seen,winrate,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(address,chain) DO UPDATE SET last_seen=excluded.last_seen,winrate=excluded.winrate,updated_at=excluded.updated_at",(w,chain,"gmgn",now,wrv,now))
   elif not is_blacklisted(c,w,chain):
    c.execute("INSERT INTO wallet_watch(address,chain,source,last_seen,winrate) VALUES(?,?,?,?,?) ON CONFLICT(address,chain) DO UPDATE SET last_seen=excluded.last_seen,winrate=excluded.winrate",(w,chain,"gmgn",now,wrv))
  after=c.execute("SELECT COUNT(*) FROM wallet_watch WHERE chain=? AND active=1",(chain,)).fetchone()[0]
  if new_w>0:
   s=f"70%+: {len(high_wr)}"+(f" ex: {high_wr[0][0]}... {high_wr[0][1]*100:.0f}%" if high_wr else "")
   emit(c,"WALLET",f"{chain} | +{new_w} новых, всего {after} | {s}")
  enter(c,chain,trades,weights,now); exits(c,chain,trades,now)
  refresh_wallet_stats(c,chain,now)
  discover_wallets(c,chain,now)
  cleanup_wallets(c,chain,now)
 c.commit()
 LOG.info("[cycle] wallets=%d events_total=%d",c.execute("SELECT COUNT(*) FROM wallet_watch").fetchone()[0],c.execute("SELECT COUNT(*) FROM engine_events").fetchone()[0])
def main():
 import argparse
 ap=argparse.ArgumentParser(); ap.add_argument("--once",action="store_true"); ap.add_argument("--db-path",default=DB); a=ap.parse_args(); logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s"); c=sqlite3.connect(a.db_path,timeout=30); init(c)
 try:
  while True:
   cycle(c)
   if a.once: break
   time.sleep(POLL)
 finally: c.close()
if __name__=="__main__": main()