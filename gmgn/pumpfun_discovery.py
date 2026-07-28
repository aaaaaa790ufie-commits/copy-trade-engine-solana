#!/usr/bin/env python3
"""Mass wallet discovery from the pump.fun launchpad.

Why this exists
---------------
GMGN's Smart Money / KOL feeds are a curated slice: a few hundred wallets at
best. This project needs tens of thousands of candidates before the weighted
convergence rule (score >= 1.0) can realistically fire on a fresh token.

pump.fun exposes its launchpad data publicly and every trade row carries the
trader's wallet address, which makes it an excellent volume source: one busy
mint yields hundreds of distinct addresses for a handful of HTTP calls.

What pump.fun does NOT give us
------------------------------
There is no win-rate, PnL or 'smart wallet' endpoint. Anything advertising
pump.fun win rates is a third-party wrapper, not the launchpad API. So the
pipeline is deliberately two-stage:

    stage 1 (cheap)  pump.fun trades       -> tens of thousands of addresses
    stage 2 (costly) GMGN portfolio stats  -> 30d win rate + activity

Only stage 2 decides whether a wallet is worth watching; stage 1 never marks a
wallet as good. Both stages are resumable because progress lives in SQLite, so
an interrupted run continues instead of starting over.

This module is read-only against both APIs and never signs or submits anything.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402  (repo-local .env loader)

LOG = logging.getLogger("pumpfun-discovery")

# --------------------------------------------------------------------------
# Settings (all overridable from the repo-local .env)
# --------------------------------------------------------------------------
API_BASE = config.get("PUMPFUN_API_BASE", "https://frontend-api-v3.pump.fun").rstrip("/")
# Optional. Public endpoints answer without it, but pump.fun's own docs recommend a
# JWT for complete responses and fewer throttles. Never logged.
AUTH_TOKEN = config.get("PUMPFUN_AUTH_TOKEN", "")
USER_AGENT = config.get("PUMPFUN_USER_AGENT", "Mozilla/5.0 (compatible; sentinel-research/1.0)")

HTTP_TIMEOUT = config.get_int("PUMPFUN_HTTP_TIMEOUT", 25)
REQUEST_DELAY = config.get_float("PUMPFUN_REQUEST_DELAY", 0.25)
MAX_RETRIES = config.get_int("PUMPFUN_MAX_RETRIES", 4)

# How many traders to request per mint from GMGN's token traders API.
# GMGN returns the top holders/traders sorted by holdings. Higher values use
# more API budget but find more wallet candidates per mint.
TRADERS_LIMIT = config.get_int("PUMPFUN_TRADERS_LIMIT", 100)
COINS_PAGE = config.get_int("PUMPFUN_COINS_PAGE", 50)

# Verification gates, matching the engine's own wallet filter.
MIN_WINRATE = config.get_float("PUMPFUN_MIN_WINRATE", 0.50)
MIN_MINTS = config.get_int("PUMPFUN_MIN_MINTS", 2)
MIN_TRADES = config.get_int("PUMPFUN_MIN_TRADES", 3)
MIN_30D_TRADES = config.get_int("PUMPFUN_MIN_30D_TRADES", 5)
STATS_BATCH = config.get_int("PUMPFUN_STATS_BATCH", 10)
STATS_DELAY = config.get_float("PUMPFUN_STATS_DELAY", 0.35)
RECHECK_SECONDS = config.get_int("PUMPFUN_RECHECK_SECONDS", 86400)

BASE58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

_stop = False


def _handle_signal(signum, frame):  # pragma: no cover - signal path
    global _stop
    _stop = True
    LOG.warning("stop requested, finishing the current page then exiting")


def valid_address(value: str) -> bool:
    return bool(value) and bool(BASE58.match(value))


# --------------------------------------------------------------------------
# pump.fun HTTP client
# --------------------------------------------------------------------------

class RateLimited(Exception):
    """429 or 5xx from pump.fun. Retried with exponential backoff."""


def _request(path: str, params: dict[str, Any] | None = None) -> Any:
    url = f"{API_BASE}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if AUTH_TOKEN:
        headers["Authorization"] = f"Bearer {AUTH_TOKEN}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            body = response.read().decode("utf-8", errors="replace")
            return json.loads(body) if body.strip() else None
    except urllib.error.HTTPError as exc:
        if exc.code == 429 or exc.code >= 500:
            raise RateLimited(f"HTTP {exc.code} for {path}") from exc
        raise
    except urllib.error.URLError as exc:
        raise RateLimited(f"network error for {path}: {exc.reason}") from exc


def api_get(path: str, params: dict[str, Any] | None = None) -> Any:
    """GET with backoff. Returns None instead of raising when an endpoint keeps failing."""
    delay = 1.0
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            payload = _request(path, params)
            time.sleep(REQUEST_DELAY)
            return payload
        except RateLimited as exc:
            if attempt == MAX_RETRIES:
                LOG.warning("giving up on %s: %s", path, exc)
                return None
            LOG.info("%s - backing off %.1fs (attempt %d/%d)", exc, delay, attempt, MAX_RETRIES)
            time.sleep(delay)
            delay = min(delay * 2, 30.0)
        except urllib.error.HTTPError as exc:
            LOG.warning("HTTP %s for %s - skipping", exc.code, path)
            return None
        except (json.JSONDecodeError, TimeoutError, OSError) as exc:
            LOG.warning("bad response for %s: %s", path, exc)
            return None
    return None


def as_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("data", "coins", "trades", "items", "results"):
            inner = payload.get(key)
            if isinstance(inner, list):
                return [x for x in inner if isinstance(x, dict)]
        return [payload]
    return []


def mint_of(row: dict[str, Any]) -> str:
    return str(row.get("mint") or row.get("address") or row.get("coin_mint") or "")

# --------------------------------------------------------------------------
# Mint discovery
# --------------------------------------------------------------------------

def iter_mints(target: int) -> list[str]:
    """Collect mints worth scraping: king of the hill, featured, live and graduated.

    Graduated coins (complete=true) matter most - a wallet that traded one and is
    still active is more interesting than one that bought a curve that never left.
    """
    seen: dict[str, None] = {}

    def take(payload: Any) -> None:
        for row in as_rows(payload):
            mint = mint_of(row)
            if valid_address(mint):
                seen.setdefault(mint, None)

    take(api_get("/coins/king-of-the-hill", {"includeNsfw": "true"}))
    for window in ("24h", "7d", "30d"):
        take(api_get(f"/coins/featured/{window}", {"limit": COINS_PAGE, "offset": 0, "includeNsfw": "true"}))

    sorts = (
        ("last_trade_timestamp", "true"),
        ("market_cap", "true"),
        ("last_trade_timestamp", "false"),
        ("created_timestamp", "false"),
    )
    for sort_key, complete in sorts:
        offset = 0
        empty_pages = 0
        while len(seen) < target and offset < 10_000 and empty_pages < 2 and not _stop:
            payload = api_get(
                "/coins",
                {
                    "offset": offset,
                    "limit": COINS_PAGE,
                    "sort": sort_key,
                    "order": "DESC",
                    "includeNsfw": "true",
                    "complete": complete,
                },
            )
            rows = as_rows(payload)
            if rows:
                empty_pages = 0
                take(rows)
            else:
                empty_pages += 1
            offset += COINS_PAGE
        if _stop or len(seen) >= target:
            break
    LOG.info("collected %d candidate mints", len(seen))
    return list(seen)[:target]


# --------------------------------------------------------------------------
# SQLite storage
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS pumpfun_candidates (
    address        TEXT PRIMARY KEY,
    trade_count    INTEGER NOT NULL DEFAULT 0,
    mint_count     INTEGER NOT NULL DEFAULT 0,
    sol_volume     REAL    NOT NULL DEFAULT 0,
    last_trade_ts  INTEGER NOT NULL DEFAULT 0,
    first_seen     INTEGER NOT NULL,
    checked_at     INTEGER NOT NULL DEFAULT 0,
    winrate        REAL    NOT NULL DEFAULT 0,
    status         TEXT    NOT NULL DEFAULT 'new'
);
CREATE INDEX IF NOT EXISTS idx_pumpfun_status
    ON pumpfun_candidates(status, mint_count DESC, sol_volume DESC);
CREATE TABLE IF NOT EXISTS pumpfun_scanned_mints (
    mint       TEXT PRIMARY KEY,
    scanned_at INTEGER NOT NULL,
    traders    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS pumpfun_wallet_mints (
    address TEXT NOT NULL,
    mint    TEXT NOT NULL,
    PRIMARY KEY (address, mint)
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def blacklisted(conn: sqlite3.Connection) -> set[str]:
    """Addresses the engine already banned. Never re-offer them for verification."""
    try:
        return {row[0] for row in conn.execute("SELECT address FROM wallet_blacklist")}
    except sqlite3.Error:
        return set()


def already_watched(conn: sqlite3.Connection) -> set[str]:
    try:
        return {row[0] for row in conn.execute("SELECT address FROM wallet_watch")}
    except sqlite3.Error:
        return set()


# --------------------------------------------------------------------------
# Stage 1 - harvest
# --------------------------------------------------------------------------

def scan_mint(conn: sqlite3.Connection, mint: str, now: int) -> int:
    """Fetch traders for a mint from GMGN and upsert every distinct wallet.

    pump.fun's own /trades/all/{mint} endpoint was deprecated (returns 404 as
    of 2026-07). GMGN's ``token traders`` returns the same data — wallet
    addresses that traded the mint — and is the same API family the engine
    already uses, so no new credentials are needed.
    """
    try:
        payload = gmgn_cli(["token", "traders", "--chain", "sol", "--address", mint, "--limit", str(TRADERS_LIMIT)])
    except Exception as exc:
        LOG.warning("token traders %s: %s", mint[:8], exc)
        return 0
    rows = stat_rows(payload)
    if not rows:
        return 0

    traders = 0
    for row in rows:
        address = str(row.get("address") or "")
        if not valid_address(address):
            continue
        trades = (int(row.get("buy_tx_count_cur", 0) or 0)
                  + int(row.get("sell_tx_count_cur", 0) or 0))
        # Skip wallets with 0 trades on this mint — they are pure holders
        # or transfer recipients, not traders we can copy.
        if trades < 1:
            continue
        last_active = int(row.get("last_active_timestamp", 0) or 0)
        # last_active_timestamp may be in milliseconds
        if last_active > 10_000_000_000:
            last_active //= 1000

        conn.execute(
            "INSERT INTO pumpfun_candidates"
            " (address, trade_count, mint_count, sol_volume, last_trade_ts, first_seen)"
            " VALUES (?, ?, 1, ?, ?, ?)"
            " ON CONFLICT(address) DO UPDATE SET"
            "   trade_count   = trade_count + excluded.trade_count,"
            "   sol_volume    = sol_volume  + excluded.sol_volume,"
            "   last_trade_ts = MAX(last_trade_ts, excluded.last_trade_ts)",
            (address, trades, 0.0, last_active, now),
        )
        cursor = conn.execute(
            "INSERT OR IGNORE INTO pumpfun_wallet_mints(address, mint) VALUES(?,?)",
            (address, mint),
        )
        if cursor.rowcount:
            conn.execute(
                "UPDATE pumpfun_candidates SET mint_count ="
                " (SELECT COUNT(*) FROM pumpfun_wallet_mints WHERE address=?) WHERE address=?",
                (address, address),
            )
        traders += 1

    conn.execute(
        "INSERT OR REPLACE INTO pumpfun_scanned_mints(mint, scanned_at, traders) VALUES(?,?,?)",
        (mint, now, traders),
    )
    conn.commit()
    return traders


def harvest(conn: sqlite3.Connection, mint_target: int, rescan_hours: int) -> int:
    now = int(time.time())
    cutoff = now - rescan_hours * 3600
    fresh = {
        row[0]
        for row in conn.execute("SELECT mint FROM pumpfun_scanned_mints WHERE scanned_at > ?", (cutoff,))
    }
    mints = [m for m in iter_mints(mint_target * 3) if m not in fresh][:mint_target]
    LOG.info("scanning %d mints (%d skipped as recently scanned)", len(mints), len(fresh))
    before = conn.execute("SELECT COUNT(*) FROM pumpfun_candidates").fetchone()[0]
    for index, mint in enumerate(mints, 1):
        if _stop:
            break
        traders = scan_mint(conn, mint, int(time.time()))
        if index % 10 == 0 or traders > 50:
            pool = conn.execute("SELECT COUNT(*) FROM pumpfun_candidates").fetchone()[0]
            LOG.info("[%d/%d] %s -> %d traders | pool: %d wallets", index, len(mints), mint[:8], traders, pool)
    after = conn.execute("SELECT COUNT(*) FROM pumpfun_candidates").fetchone()[0]
    LOG.info("harvest done: %d new wallets (pool now %d)", after - before, after)
    return after - before


# --------------------------------------------------------------------------
# Stage 2 - verify win rate through GMGN
# --------------------------------------------------------------------------

def gmgn_cli(args: list[str]) -> Any:
    binary = shutil.which("gmgn-cli") or shutil.which("gmgn-cli.cmd")
    if not binary:
        raise RuntimeError("gmgn-cli not found - npm install -g gmgn-cli")
    proc = subprocess.run(
        [binary, *args, "--raw"],
        capture_output=True,
        text=True,
        timeout=90,
        env=config.gmgn_env(),
    )
    if proc.returncode:
        raise RuntimeError((proc.stderr or proc.stdout).strip()[:400])
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    return json.loads(lines[-1]) if lines else {}


def stat_rows(payload: Any) -> list[dict[str, Any]]:
    while isinstance(payload, dict) and isinstance(payload.get("data"), (dict, list)):
        payload = payload["data"]
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("list", "items", "result"):
            if isinstance(payload.get(key), list):
                return [x for x in payload[key] if isinstance(x, dict)]
        return [payload] if payload else []
    return []


def number(row: dict[str, Any], *keys: str) -> float:
    for key in keys:
        value: Any = row
        for part in key.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        if value is None:
            continue
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if "winrate" in key.lower() and parsed > 1:
            parsed /= 100.0
        return parsed
    return 0.0


def pending_batch(conn: sqlite3.Connection, limit: int, banned: set[str], watched: set[str]) -> list[str]:
    """Highest-signal unverified wallets first: many distinct mints, then volume.

    Ordering matters more than it looks. Verification is the expensive half, so
    spending it on wallets that touched several different coins finds real traders
    far faster than walking the pool in insertion order.
    """
    stale = int(time.time()) - RECHECK_SECONDS
    rows = conn.execute(
        "SELECT address FROM pumpfun_candidates"
        " WHERE mint_count >= ? AND trade_count >= ?"
        "   AND (status = 'new' OR (status = 'ok' AND checked_at < ?))"
        " ORDER BY mint_count DESC, sol_volume DESC"
        " LIMIT ?",
        (MIN_MINTS, MIN_TRADES, stale, limit * 3),
    ).fetchall()
    out: list[str] = []
    for (address,) in rows:
        if address in banned or address in watched:
            conn.execute("UPDATE pumpfun_candidates SET status='known' WHERE address=?", (address,))
            continue
        out.append(address)
        if len(out) >= limit:
            break
    conn.commit()
    return out


def verify(conn: sqlite3.Connection, limit: int) -> tuple[int, int]:
    banned = blacklisted(conn)
    watched = already_watched(conn)
    batch = pending_batch(conn, limit, banned, watched)
    if not batch:
        LOG.info("nothing left to verify - raise --harvest-mints to grow the pool")
        return 0, 0
    LOG.info("verifying %d wallets through GMGN", len(batch))
    checked = accepted = 0
    now = int(time.time())
    for start in range(0, len(batch), STATS_BATCH):
        if _stop:
            break
        chunk = batch[start : start + STATS_BATCH]
        try:
            payload = gmgn_cli(["portfolio", "stats", "--chain", "sol", "--wallet", *chunk, "--period", "30d"])
        except Exception as exc:  # noqa: BLE001 - one bad batch must not kill the run
            LOG.warning("stats batch failed: %s", exc)
            time.sleep(STATS_DELAY * 4)
            continue
        rows = stat_rows(payload)
        by_address: dict[str, dict[str, Any]] = {}
        for row in rows:
            key = str(row.get("wallet") or row.get("address") or row.get("wallet_address") or "")
            if key:
                by_address[key] = row
        if len(chunk) == 1 and len(rows) == 1 and chunk[0] not in by_address:
            by_address[chunk[0]] = rows[0]

        for address in chunk:
            checked += 1
            stat = by_address.get(address)
            if not stat:
                # No data is not the same as a bad wallet. Leave it 'new' so a later
                # pass retries instead of silently discarding it.
                continue
            winrate = number(stat, "winrate", "win_rate", "pnl_stat.winrate")
            trades_30d = number(stat, "buy_count_30d", "txs_30d", "trades_30d", "buy_count")
            good = winrate >= MIN_WINRATE and trades_30d >= MIN_30D_TRADES
            conn.execute(
                "UPDATE pumpfun_candidates SET checked_at=?, winrate=?, status=? WHERE address=?",
                (now, winrate, "ok" if good else "rejected", address),
            )
            if good:
                accepted += 1
                conn.execute(
                    "INSERT INTO wallet_watch(address, chain, source, last_seen, winrate, updated_at)"
                    " VALUES(?,?,?,?,?,?)"
                    " ON CONFLICT(address, chain) DO UPDATE SET"
                    "   winrate = excluded.winrate,"
                    "   updated_at = excluded.updated_at",
                    (address, "sol", "pumpfun", now, winrate, now),
                )
        conn.commit()
        time.sleep(STATS_DELAY)
    LOG.info("verified %d wallets, %d passed the %.0f%% gate", checked, accepted, MIN_WINRATE * 100)
    return checked, accepted


# --------------------------------------------------------------------------
# Export and reporting
# --------------------------------------------------------------------------

def export(conn: sqlite3.Connection, path: Path) -> int:
    rows = conn.execute(
        "SELECT address, winrate, last_trade_ts FROM pumpfun_candidates"
        " WHERE status='ok' AND winrate >= ?"
        " ORDER BY winrate DESC, sol_volume DESC",
        (MIN_WINRATE,),
    ).fetchall()
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    lines = [
        f"# pump.fun harvested wallets | winrate>={MIN_WINRATE:.2f} | {len(rows)} wallets | {stamp}Z",
        "# address | source | winrate | last_trade_ts",
    ]
    lines.extend(f"{a} | pumpfun | {w:.4f} | {t}" for a, w, t in rows)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(temp, path)
    LOG.info("wrote %d verified wallets to %s", len(rows), path)
    return len(rows)


def report(conn: sqlite3.Connection) -> None:
    total, new, ok, rejected, known = conn.execute(
        "SELECT COUNT(*), SUM(status='new'), SUM(status='ok'),"
        " SUM(status='rejected'), SUM(status='known') FROM pumpfun_candidates"
    ).fetchone()
    mints = conn.execute("SELECT COUNT(*) FROM pumpfun_scanned_mints").fetchone()[0]
    LOG.info(
        "pool: %s wallets | unverified %s | verified-good %s | rejected %s | already-known %s | mints scanned %s",
        total or 0, new or 0, ok or 0, rejected or 0, known or 0, mints,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Harvest pump.fun traders, verify win rate through GMGN")
    parser.add_argument("--db-path", default=config.DB_PATH)
    parser.add_argument("--harvest-mints", type=int, default=300, help="mints to scrape this run (0 skips harvesting)")
    parser.add_argument("--verify", type=int, default=2000, help="wallets to verify this run (0 skips verification)")
    parser.add_argument("--rescan-hours", type=int, default=6, help="do not rescan a mint seen within N hours")
    parser.add_argument("--export", type=Path, default=Path(config.ROOT) / "wallets-pumpfun.txt")
    parser.add_argument("--loop", action="store_true", help="keep harvesting and verifying until stopped")
    parser.add_argument("--loop-sleep", type=int, default=300)
    parser.add_argument("--status", action="store_true", help="print pool counters and exit")
    args = parser.parse_args()

    config.use_utf8_stdio()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    conn = connect(args.db_path)
    try:
        if args.status:
            report(conn)
            return 0
        while True:
            if args.harvest_mints:
                harvest(conn, args.harvest_mints, args.rescan_hours)
            if args.verify and not _stop:
                verify(conn, args.verify)
            export(conn, args.export)
            report(conn)
            if not args.loop or _stop:
                break
            LOG.info("sleeping %ds before the next round", args.loop_sleep)
            for _ in range(args.loop_sleep):
                if _stop:
                    break
                time.sleep(1)
            if _stop:
                break
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
