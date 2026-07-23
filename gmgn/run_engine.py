#!/usr/bin/env python3
from __future__ import annotations
import argparse, logging, os, sqlite3, time
from engine import DB, POLL, cycle, init

def import_seeds(c):
    path=os.getenv('SOLANA_SEED_FILE','data/seed_wallets_sol.txt')
    if not os.path.exists(path): return
    now=int(time.time())
    for line in open(path,encoding='utf-8'):
        address=line.strip()
        if address and not address.startswith('#'):
            c.execute("INSERT OR IGNORE INTO wallet_watch(address,chain,source,last_seen,winrate,updated_at) VALUES(?,?,?,?,?,?)",(address,'sol','manual_seed',0,0,now))
    c.commit()

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--once',action='store_true'); ap.add_argument('--db-path',default=DB); args=ap.parse_args()
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    c=sqlite3.connect(args.db_path,timeout=30); init(c); import_seeds(c)
    try:
        while True:
            cycle(c)
            if args.once: break
            time.sleep(POLL)
    finally: c.close()
if __name__=='__main__': main()
