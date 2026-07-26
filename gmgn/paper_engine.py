#!/usr/bin/env python3
from __future__ import annotations
import json, logging, os, re, shutil, sqlite3, subprocess, time
from collections import defaultdict
import config
LOG=logging.getLogger("paper-engine")
# All tunables and credentials come from the repo-local .env via config.py.
DB=config.DB_PATH; BUDGET=config.BUDGET_SOL; STAKE=config.STAKE_SOL; WINDOW=config.CLUSTER_WINDOW; COOLDOWN=config.COOLDOWN_SECONDS; POLL=config.POLL_SECONDS; ENTRY=config.ENTRY_SCORE; TRAIL_ACT=config.TRAILING_ACTIVATE_PCT/100; TRAIL_DIST=config.TRAILING_DISTANCE_PCT/100; HARD=config.HARD_STOP_PCT/100; LIMIT=config.FEED_LIMIT; CHAINS=config.CHAINS
MAX_HOLD=config.MAX_HOLD_SECONDS; ZERO_TTL=config.ZERO_WINRATE_TTL; PRICE_TTL=config.PRICE_TTL
def _find_gmgn():
    """Resolve the gmgn-cli entrypoint. On Windows CreateProcess needs the .cmd shim
    by name, and npm's global bin is often missing from a non-login shell's PATH."""
    for name in ("gmgn-cli","gmgn-cli.cmd"):
        found=shutil.which(name)
        if found: return found
    if os.name=="nt":
        npm_bin=os.path.expandvars(r"%APPDATA%\npm\gmgn-cli.cmd")
        if os.path.isfile(npm_bin): return npm_bin
    return "gmgn-cli.cmd" if os.name=="nt" else "gmgn-cli"
_GMGN=_find_gmgn()
CLI_TIMEOUT=config.get_int("GMGN_CLI_TIMEOUT",45)
def gmgn_cli(args,timeout=None):
 """Run gmgn-cli with the repo-local credentials and return its parsed JSON.

 Shared with mass_discovery so there is one place that decides where the binary is
 and which credentials it sees — the env= is what keeps every API key inside the
 project directory instead of falling back to ~/.config/gmgn."""
 # encoding must be explicit: text=True decodes with the locale codec, and on a Russian
 # Windows that is cp1251, which raises UnicodeDecodeError the moment a token name
 # contains an emoji or CJK character — a crash on the engine's main API path.
 p=subprocess.run([_GMGN,*args,"--raw"],capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=timeout or CLI_TIMEOUT,env=config.gmgn_env())
 if p.returncode: raise RuntimeError((p.stderr or p.stdout or "").strip() or f"gmgn-cli exited {p.returncode}")
 lines=[x.strip() for x in (p.stdout or "").splitlines() if x.strip()]
 if not lines: return {}
 try: return json.loads(lines[-1])
 except json.JSONDecodeError as e: raise RuntimeError(f"gmgn-cli returned non-JSON: {lines[-1][:120]}") from e
def cli(args): return gmgn_cli(args)
def list_rows(x):
 if isinstance(x,dict) and isinstance(x.get("data"),dict): x=x["data"]
 if isinstance(x,dict) and isinstance(x.get("list"),list): x=x["list"]
 return x if isinstance(x,list) else ([x] if isinstance(x,dict) and x else [])
def _is_winrate_key(k):
 """True for winrate/win_rate/pnl_stat.win_rate alike.

 The underscore matters: a plain `"winrate" in k` test is False for "win_rate", so a
 percentage arriving under that spelling was never divided by 100. A wallet at 75%
 became winrate=75.0, which cleared the >=0.90 elite gate, displayed as 7500% and
 broke the small-sample check. GMGN currently sends "winrate", but the alternate
 spellings exist precisely because the response shape has varied."""
 return "winrate" in k.replace("_","").lower()
def n(o,*keys):
 for k in keys:
  v=o
  for part in k.split("."): v=v.get(part) if isinstance(v,dict) else None
  if v is not None:
   try: f=float(v)
   except (TypeError,ValueError): continue
   if _is_winrate_key(k):
    if f>1: f/=100
    # Still out of range means the field is not what we think it is; a bogus win rate
    # would silently promote a wallet to elite, so refuse it rather than store it.
    if not 0<=f<=1:
     LOG.warning("ignoring out-of-range winrate %r from %s",v,k); continue
   return f
 return 0.0
# Everything the feed returns is untrusted: it reaches the database and from there the
# Mini App. Solana addresses are base58, 32-44 chars — anything else is rejected at the
# boundary rather than stored and rendered later.
_ADDR_RE=re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
def valid_address(a):
 return bool(a) and bool(_ADDR_RE.match(a))
def _addr(t,*keys):
 for k in keys:
  v=t.get(k)
  if v is None: continue
  v=str(v)
  if valid_address(v): return v
  if v: LOG.debug("rejected malformed address %r from field %s",v[:64],k)
 return ""
def wallet(t): return _addr(t,"maker","wallet")
def mint(t): return _addr(t,"base_address","token_address")
def stamp(t): return int(n(t,"timestamp","trigger_at"))
def quote(t): return str(t.get("side","")).lower()
def wr(s): return n(s,"winrate","win_rate","pnl_stat.winrate")
def weight(x): return .25 if x>=.70 else .0625 if x>=.60 else .03125 if x>=.50 else 0.0
def px(t): return n(t,"price_now","price_usd","price")
_price_cache={}
PRICE_CACHE_MAX=config.get_int("GMGN_PRICE_CACHE_MAX",5000)
def _cache_price(key,now,p):
 """Store a mark, keeping the cache bounded.

 The engine prices every token it sees, so an unbounded dict grows for as long as the
 process lives. Expired entries are dropped first; if that frees nothing, the oldest
 half goes."""
 if len(_price_cache)>=PRICE_CACHE_MAX:
  for k in [k for k,(ts,_) in _price_cache.items() if now-ts>=PRICE_TTL]: del _price_cache[k]
  if len(_price_cache)>=PRICE_CACHE_MAX:
   for k in sorted(_price_cache,key=lambda k:_price_cache[k][0])[:len(_price_cache)//2]: del _price_cache[k]
 _price_cache[key]=(now,p)
def token_price(chain,mint):
 """Independent mark price from `token info` (cached PRICE_TTL sec); 0.0 when unavailable."""
 now=time.time(); hit=_price_cache.get((chain,mint))
 if hit and now-hit[0]<PRICE_TTL: return hit[1]
 p=0.0
 try:
  row=(list_rows(cli(["token","info","--chain",chain,"--address",mint])) or [{}])[0]
  raw_price=row.get("price")
  # GMGN returns price either as a scalar or as {"address":..., "price":"0.0000013"}.
  if isinstance(raw_price,dict): raw_price=raw_price.get("price") or raw_price.get("price_usd")
  if raw_price is not None:
   try: p=float(raw_price)
   except (TypeError,ValueError): LOG.debug("unparsable price for %s: %r",mint[:8],raw_price)
  if p<=0: p=n(row,"price_usd","usd_price","price_now")
 except Exception as e: LOG.warning("price %s %s: %s",chain,mint[:8],e)
 if p<0:
  LOG.warning("negative price %r for %s — treating as unavailable",p,mint[:8]); p=0.0
 _cache_price((chain,mint),now,p); return p
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
CREATE TABLE IF NOT EXISTS token_scores(chain TEXT NOT NULL,token_mint TEXT NOT NULL,score REAL NOT NULL,buy_wallets INTEGER NOT NULL,total_wallets INTEGER NOT NULL,updated_at INTEGER NOT NULL,PRIMARY KEY(chain,token_mint));
CREATE TABLE IF NOT EXISTS engine_events(id INTEGER PRIMARY KEY AUTOINCREMENT,event_ts INTEGER NOT NULL,kind TEXT NOT NULL,message TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS engine_state(key TEXT PRIMARY KEY,value TEXT NOT NULL,updated_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_trades_ts ON paper_trades(event_ts);
CREATE INDEX IF NOT EXISTS idx_events_ts ON engine_events(event_ts);
CREATE INDEX IF NOT EXISTS idx_watch_winrate ON wallet_watch(chain,active,winrate);
"""); c.commit()
def emit(c,kind,msg): LOG.info("%s: %s",kind,msg); c.execute("INSERT INTO engine_events VALUES(NULL,?,?,?)",(int(time.time()),kind,msg))
def is_blacklisted(c,addr,chain): return bool(c.execute("SELECT 1 FROM wallet_blacklist WHERE address=? AND chain=?",(addr,chain)).fetchone())
def get_stats(chain,wallets,max_batches=0):
 """Fetch portfolio stats in batches of 10. `max_batches` caps the work per call so a
 slow or timing-out API can never stall the loop that enforces stop-losses."""
 out={}
 batches=[wallets[i:i+10] for i in range(0,len(wallets),10)]
 if max_batches>0: batches=batches[:max_batches]
 for b in batches:
  try: got=list_rows(cli(["portfolio","stats","--chain",chain,"--wallet",*b,"--period","30d"]))
  except Exception as e: LOG.warning("stats %s: %s",chain,e); continue
  for r in got:
   a=str(r.get("address") or r.get("wallet") or r.get("wallet_address") or r.get("maker") or "")
   if a: out[a]=r
  if len(b)==1 and b[0] not in out and len(got)==1: out[b[0]]=got[0]
  time.sleep(0.25)
 return out
STATS_REFRESH_SEC=config.STATS_TTL
# Wallet bookkeeping is background work: bounded per pass so it can never crowd out
# the price checks that close positions.
STATS_BATCH_MAX=config.get_int("GMGN_STATS_BATCH_MAX",6)
def refresh_wallet_stats(c,chain,now):
 stale=c.execute("SELECT address FROM wallet_watch WHERE chain=? AND (winrate=0 OR ?-updated_at>=?) ORDER BY updated_at LIMIT ?",(chain,now,STATS_REFRESH_SEC,STATS_BATCH_MAX*10)).fetchall()
 if not stale: return 0
 addrs=[r[0] for r in stale]; st=get_stats(chain,addrs,max_batches=STATS_BATCH_MAX); upd=0; new_high=[]
 for w,data in st.items():
  wrv=wr(data); bc=int(n(data,"buy","buy_count","trades_7d")); sc=int(n(data,"sell","sell_count")); total_buys=max(bc,sc)
  # Ineligible is not the same as bad. A wallet with too small a sample, or one that
  # has stopped buying, is parked with active=0 — it keeps its real winrate, carries no
  # weight, and is re-checked later. Overwriting the winrate with a synthetic 0.49 used
  # to hand these to cleanup_wallets, which banned them permanently.
  if (wrv>=1.0 and bc+sc<=1) or (wrv>0 and bc==0):
   c.execute("UPDATE wallet_watch SET winrate=?,active=0,updated_at=? WHERE address=? AND chain=?",(wrv,now,w,chain)); upd+=1; continue
  if wrv>0 and total_buys>0:
   old=c.execute("SELECT winrate FROM wallet_watch WHERE address=? AND chain=?",(w,chain)).fetchone()
   ls=int(n(data,"last_timestamp"))
   # active=1 also restores a parked wallet that has resumed trading.
   if ls>0: c.execute("UPDATE wallet_watch SET winrate=?,last_seen=?,active=1,updated_at=? WHERE address=? AND chain=?",(wrv,ls,now,w,chain))
   else: c.execute("UPDATE wallet_watch SET winrate=?,active=1,updated_at=? WHERE address=? AND chain=?",(wrv,now,w,chain))
   upd+=1
   if wrv>=.70 and old and old[0]<.70: new_high.append((w[:8],wrv))
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
 rate limit can never blacklist a good wallet forever. Manual seeds are never auto-dropped.

 Parked wallets (active=0: too small a sample, or no recent buys) are deliberately excluded.
 They are not bad traders, and a blacklisted address is never re-added by discovery."""
 low=c.execute("SELECT address FROM wallet_watch WHERE chain=? AND active=1 AND winrate>0 AND winrate<0.50",(chain,)).fetchall()
 if low:
  c.executemany("INSERT OR IGNORE INTO wallet_blacklist(address,chain,blacklisted_at,reason) VALUES(?,?,?,'low_winrate')",[(r[0],chain,now) for r in low])
  c.executemany("DELETE FROM wallet_watch WHERE chain=? AND address=?",[(chain,r[0]) for r in low])
 c.execute("DELETE FROM wallet_watch WHERE chain=? AND winrate=0 AND source!='manual_seed' AND ?-updated_at>=?",(chain,now,ZERO_TTL))
def cooling(c,m,chain,now):
 r=c.execute("SELECT until_ts FROM paper_cooldowns WHERE token_mint=? AND chain=?",(m,chain)).fetchone(); return bool(r and r[0]>now)
# A position whose token stops being priceable never closes, so its stake stays locked
# out of the account. Alert once it is clearly stuck, then only occasionally — the poll
# loop runs every POLL seconds and would otherwise flood Telegram.
STUCK_AFTER=config.get_int("GMGN_STUCK_AFTER_SECONDS",MAX_HOLD*2)
STUCK_REMIND=config.get_int("GMGN_STUCK_REMIND_SECONDS",21600)
def throttled(c,key,now,interval):
 """True at most once per `interval` for this key. Used to rate-limit repeat alerts."""
 row=c.execute("SELECT updated_at FROM engine_state WHERE key=?",(key,)).fetchone()
 if row and now-row[0]<interval: return False
 c.execute("INSERT INTO engine_state(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",(key,str(now),now))
 return True
def cluster(chain,trades,weights,now):
 """Latest action per wallet, per mint, inside the cluster window.

 Only weighted wallets count, and only their most recent action: a wallet that bought
 and then sold is a seller, not a buyer. enter() and save_token_scores() must build
 exactly the same view — otherwise the score /weights reports in Telegram would not be
 the score that actually triggers an entry."""
 latest=defaultdict(dict)
 for t in trades:
  if allowed(t,chain) and mint(t) and stamp(t)>=now-WINDOW and wallet(t) in weights and quote(t) in ("buy","sell"):
   if wallet(t) not in latest[mint(t)] or stamp(t)>stamp(latest[mint(t)][wallet(t)]): latest[mint(t)][wallet(t)]=t
 return latest
def score_of(ws,weights):
 """Buying wallets for one mint and their summed weight."""
 buys={w:t for w,t in ws.items() if quote(t)=="buy"}
 return buys,sum(weights[w] for w in buys)
def enter(c,chain,trades,weights,now):
 latest=cluster(chain,trades,weights,now)
 open_mints={r[0] for r in c.execute("SELECT token_mint FROM paper_positions WHERE status='open'")}
 for m,ws in latest.items():
  buys,score=score_of(ws,weights)
  if score<ENTRY or m in open_mints or cooling(c,m,chain,now): continue
  p=token_price(chain,m) or (px(max(buys.values(),key=stamp)) if buys else 0); a=c.execute("SELECT budget_sol,bankrupt FROM paper_account WHERE id=1").fetchone()
  if p<=0 or not a: continue
  if a[0]<STAKE:
   fully_invested=bool(c.execute("SELECT 1 FROM paper_positions WHERE status='open' LIMIT 1").fetchone())
   if not a[1] and not fully_invested: c.execute("UPDATE paper_account SET bankrupt=1,updated_at=? WHERE id=1",(now,)); emit(c,"BANKRUPT","обнулились в papertrading, скажи это своему hermes agent, будем разбираться по сделкам")
   continue
  c.execute("UPDATE paper_account SET budget_sol=budget_sol-?,updated_at=? WHERE id=1",(STAKE,now))
  # token_mint is the primary key, so a token traded before still has its closed row.
  # Reopening reuses it — the trade journal in paper_trades keeps the full history.
  c.execute("INSERT INTO paper_positions(token_mint,chain,entry_price,peak_price,stake_sol,opened_at,signal_score,wallet_count,status) VALUES(?,?,?,?,?,?,?,?,'open') ON CONFLICT(token_mint) DO UPDATE SET chain=excluded.chain,entry_price=excluded.entry_price,peak_price=excluded.peak_price,stake_sol=excluded.stake_sol,opened_at=excluded.opened_at,signal_score=excluded.signal_score,wallet_count=excluded.wallet_count,status='open'",(m,chain,p,p,STAKE,now,score,len(buys)))
  c.execute("INSERT INTO paper_trades(token_mint,chain,action,price,stake_sol,reason,wallet_count,signal_score,event_ts) VALUES(?,?,?,?,?,?,?,?,?)",(m,chain,"ENTRY",p,STAKE,"weighted cluster",len(buys),score,now)); emit(c,"ENTRY",f"{chain} {m} | wallets={len(buys)} score={score:.4f} | {STAKE:.4f} SOL")
def exits(c,chain,trades,now):
 latest={}
 for t in trades:
  if allowed(t,chain) and mint(t) and px(t)>0 and (mint(t) not in latest or stamp(t)>stamp(latest[mint(t)])): latest[mint(t)]=t
 positions=c.execute("SELECT token_mint,entry_price,peak_price,stake_sol,signal_score,wallet_count,opened_at FROM paper_positions WHERE chain=? AND status=?",(chain,"open")).fetchall()
 for m,entry,peak,stake,score,count,opened in positions:
  if entry<=0:
   # Would raise ZeroDivisionError below and abort the loop, leaving every later
   # position in this chain unchecked. enter() rejects a zero entry price, so this
   # can only come from hand-edited or legacy data — skip the row, keep the loop.
   LOG.error("position %s has a non-positive entry price %r; skipping it",m[:8],entry); continue
  current=token_price(chain,m)
  if current<=0 and m in latest: current=px(latest[m])
  expired=now-opened>=MAX_HOLD
  if current<=0:
   if expired:
    LOG.warning("position %s past max hold but no price available; retrying next cycle",m[:8])
    # A token that stays unpriceable is almost certainly delisted, and its stake is
    # locked out of the account for as long as it stays open. Valuing it is the
    # operator's call (see ISSUES.md), so surface it loudly instead of guessing.
    if now-opened>=STUCK_AFTER and throttled(c,f"stuck_{m}",now,STUCK_REMIND):
     emit(c,"STUCK",f"{chain} {m} | {(now-opened)//3600}ч без котировки, ставка {stake:.4f} SOL заморожена")
   continue
  peak=max(peak,current); change=current/entry-1; c.execute("UPDATE paper_positions SET peak_price=? WHERE token_mint=?",(peak,m)); hard=change<=-HARD; trailing=(peak/entry-1)>=TRAIL_ACT and current<=peak*(1-TRAIL_DIST)
  if hard or trailing or expired:
   reason=f"hard stop -{HARD*100:.0f}%" if hard else (f"trailing stop {TRAIL_DIST*100:.0f}%" if trailing else f"max hold {MAX_HOLD//3600}h"); pnl=stake*change; c.execute("UPDATE paper_account SET budget_sol=budget_sol+?,updated_at=? WHERE id=1",(stake+pnl,now)); c.execute("UPDATE paper_positions SET status='closed' WHERE token_mint=?",(m,)); c.execute("INSERT INTO paper_cooldowns VALUES(?,?,?) ON CONFLICT(token_mint,chain) DO UPDATE SET until_ts=excluded.until_ts",(m,chain,now+COOLDOWN)); c.execute("INSERT INTO paper_trades(token_mint,chain,action,price,stake_sol,pnl_sol,pnl_pct,reason,wallet_count,signal_score,event_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?)",(m,chain,"EXIT",current,stake,pnl,change,reason,count,score,now)); emit(c,"EXIT",f"{chain} {m} | {change*100:.2f}% ({pnl:+.5f} SOL) | {reason}")
 a=c.execute("SELECT budget_sol,bankrupt FROM paper_account WHERE id=1").fetchone()
 if a and a[1] and a[0]>=STAKE:
  c.execute("UPDATE paper_account SET bankrupt=0,updated_at=? WHERE id=1",(now,)); emit(c,"RECOVERY",f"баланс {a[0]:.5f} SOL снова покрывает ставку {STAKE:.4f} — paper-трейдинг возобновлён")
def save_token_scores(c,chain,trades,weights,now):
 """Publish the same cluster view enter() acts on, so /weights matches reality."""
 c.execute("DELETE FROM token_scores WHERE chain=?",(chain,))
 for m,ws in cluster(chain,trades,weights,now).items():
  buys,score=score_of(ws,weights)
  if score>0:
   c.execute("INSERT INTO token_scores(chain,token_mint,score,buy_wallets,total_wallets,updated_at) VALUES(?,?,?,?,?,?)",(chain,m,score,len(buys),len(ws),now))
MAINT_INTERVAL=config.get_int("GMGN_MAINTENANCE_SECONDS",600)
_last_maint={}
def cached_weights(c,chain):
 """Weights straight from the winrates already stored in wallet_watch.

 The feed hands us ~200 makers per poll; asking the stats API about all of them took
 up to 20 round-trips of 45s each, which is what used to delay stop-loss checks by
 tens of minutes. Winrates barely move over an hour, so the cached value is just as
 good a weight, and refresh_wallet_stats keeps it current in the background."""
 return {a:weight(w) for a,w in c.execute("SELECT address,winrate FROM wallet_watch WHERE chain=? AND active=1 AND winrate>=0.50",(chain,)) if weight(w)>0}
def learn_new_makers(c,chain,trades,now):
 """Look up only makers we have never scored before, a few batches per cycle."""
 seen={r[0] for r in c.execute("SELECT address FROM wallet_watch WHERE chain=?",(chain,))}
 unknown=sorted({wallet(t) for t in trades if wallet(t)} - seen)
 if not unknown: return {}
 stats=get_stats(chain,unknown,max_batches=STATS_BATCH_MAX); new_w=0; high_wr=[]
 for w,s in stats.items():
  wrv=wr(s)
  if wrv<=0 or is_blacklisted(c,w,chain): continue
  c.execute("INSERT INTO wallet_watch(address,chain,source,last_seen,winrate,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(address,chain) DO UPDATE SET last_seen=excluded.last_seen,winrate=excluded.winrate,updated_at=excluded.updated_at",(w,chain,"gmgn",now,wrv,now))
  new_w+=1
  if wrv>=.70: high_wr.append((w[:8],wrv))
 if new_w:
  total=c.execute("SELECT COUNT(*) FROM wallet_watch WHERE chain=? AND active=1",(chain,)).fetchone()[0]
  s=f"70%+: {len(high_wr)}"+(f" ex: {high_wr[0][0]}... {high_wr[0][1]*100:.0f}%" if high_wr else "")
  emit(c,"WALLET",f"{chain} | +{new_w} новых, всего {total} | {s}")
 return stats
def heartbeat(c,now,detail=""):
 c.execute("INSERT INTO engine_state(key,value,updated_at) VALUES('last_cycle',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",(detail or str(now),now))
def last_cycle_ts(c):
 row=c.execute("SELECT updated_at FROM engine_state WHERE key='last_cycle'").fetchone()
 return row[0] if row else 0
ELITE_WINRATE=config.get_float("GMGN_ELITE_WINRATE",0.90)
ELITE_CALLOUTS_MAX=config.get_int("GMGN_ELITE_CALLOUTS_MAX",10)
def elite_buy_callouts(c,chain,trades,now,since):
 """Announce a top-winrate wallet buying into a token.

 The feed replays its last few hundred trades on every poll, so this has to fire once
 per trade, not once per poll: only trades newer than the previous cycle count. On the
 first cycle after a start there is no previous cycle to compare against, so nothing is
 announced rather than replaying the whole feed. A long outage is likewise clamped to
 one cluster window, and the batch is capped, so a backlog cannot flood Telegram.

 Eligibility comes from wallet_watch, not from the freshly fetched stats of newly seen
 makers — otherwise an already-known elite wallet would never trigger a call-out."""
 if not since: return
 cutoff=max(since,now-WINDOW)
 elite=dict(c.execute("SELECT address,winrate FROM wallet_watch WHERE chain=? AND active=1 AND winrate>=?",(chain,ELITE_WINRATE)))
 if not elite: return
 seen=set(); sent=0
 for t in sorted(trades,key=stamp,reverse=True):
  w=wallet(t); m=mint(t)
  if not (w in elite and m and quote(t)=="buy" and stamp(t)>cutoff): continue
  if (w,m) in seen: continue
  seen.add((w,m))
  emit(c,"WALLET_BUY",f"кошелёк {w[:12]}... зашёл в монету {m} (wr={elite[w]*100:.0f}%)")
  sent+=1
  if sent>=ELITE_CALLOUTS_MAX:
   LOG.info("elite call-outs capped at %d this cycle on %s",ELITE_CALLOUTS_MAX,chain); return
def cycle(c):
 now=int(time.time()); since=last_cycle_ts(c)
 for chain in CHAINS:
  # 1. Feed first — one fast call, and the only hard dependency of the exit path.
  try: trades=list_rows(cli(["track","smartmoney","--chain",chain,"--limit",str(LIMIT)]))
  except Exception as e:
   LOG.warning("feed %s: %s",chain,e); trades=[]
  # 2. Stops before anything else. Nothing slow may run ahead of this.
  exits(c,chain,trades,now); c.commit()
  if not trades: continue
  # 3. Entry decisions off cached winrates, plus a bounded lookup of unseen makers.
  learn_new_makers(c,chain,trades,now)
  weights=cached_weights(c,chain)
  enter(c,chain,trades,weights,now); save_token_scores(c,chain,trades,weights,now)
  elite_buy_callouts(c,chain,trades,now,since)
  # 4. Background bookkeeping, throttled so it cannot dominate the loop. The last run is
  # persisted so a restart loop cannot hammer the stats API.
  if chain not in _last_maint:
   row=c.execute("SELECT value FROM engine_state WHERE key=?",(f"maint_{chain}",)).fetchone()
   _last_maint[chain]=int(row[0]) if row and str(row[0]).isdigit() else 0
  if now-_last_maint.get(chain,0)>=MAINT_INTERVAL:
   _last_maint[chain]=now
   c.execute("INSERT INTO engine_state(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",(f"maint_{chain}",str(now),now))
   refresh_wallet_stats(c,chain,now); discover_wallets(c,chain,now); cleanup_wallets(c,chain,now)
 heartbeat(c,now); c.commit()
 LOG.info("[cycle] %.1fs wallets=%d open=%d",time.time()-now,c.execute("SELECT COUNT(*) FROM wallet_watch").fetchone()[0],c.execute("SELECT COUNT(*) FROM paper_positions WHERE status='open'").fetchone()[0])
MAX_CYCLE_FAILURES=config.get_int("GMGN_MAX_CYCLE_FAILURES",5)
def run_forever(c,once=False):
 """Poll loop that survives a transient cycle failure.

 A single bad API response used to raise out of cycle() and kill the process. The
 supervisor would restart it, but every second spent restarting is a second in which
 open positions are not checked against their stops — the exact failure that produced
 the -99.99% exits. So a failed cycle is rolled back, journalled and retried, and only
 a persistent failure (MAX_CYCLE_FAILURES in a row) is escalated to the supervisor for
 a clean restart. Failures are never swallowed silently: each one is logged with its
 traceback and pushed to Telegram as an ERROR event."""
 failures=0
 while True:
  try:
   cycle(c); failures=0
  except KeyboardInterrupt: raise
  except Exception as e:
   failures+=1
   try: c.rollback()   # discard the half-applied transaction, e.g. a debited stake
   except Exception: LOG.exception("rollback failed")
   LOG.exception("cycle failed (%d/%d consecutive)",failures,MAX_CYCLE_FAILURES)
   try:
    emit(c,"ERROR",f"цикл упал ({failures}/{MAX_CYCLE_FAILURES}): {type(e).__name__}: {e}"); c.commit()
   except Exception: LOG.exception("could not journal the cycle failure")
   if failures>=MAX_CYCLE_FAILURES:
    LOG.error("%d consecutive failures — exiting for a clean restart",failures)
    raise
  if once: break
  time.sleep(POLL)
def main():
 import argparse
 ap=argparse.ArgumentParser(); ap.add_argument("--once",action="store_true"); ap.add_argument("--db-path",default=DB); a=ap.parse_args(); config.use_utf8_stdio(); logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s"); c=sqlite3.connect(a.db_path,timeout=30); init(c)
 try: run_forever(c,once=a.once)
 finally: c.close()
if __name__=="__main__": main()