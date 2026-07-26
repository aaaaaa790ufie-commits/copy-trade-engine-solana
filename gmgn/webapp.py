#!/usr/bin/env python3
"""Local HTTP server backing the Sentinel Telegram Mini App.

Serves the single-page UI from `gmgn/webapp/` plus a read-only JSON API over the
paper-trading SQLite database. Nothing here can place a trade or move funds — it
is a viewer.

Run standalone:

    python gmgn/webapp.py            # http://127.0.0.1:8770

To open it as a Telegram Mini App the page needs a public HTTPS origin, e.g.

    cloudflared tunnel --url http://127.0.0.1:8770

then put the resulting https URL into `WEBAPP_PUBLIC_URL` in `.env` and restart
the bot, which will attach a "Открыть панель" WebApp button.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import sqlite3
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402

LOG = logging.getLogger("webapp")
STATIC_DIR = Path(__file__).resolve().parent / "webapp"

# initData signatures older than this are rejected even if the HMAC is valid,
# so a captured URL cannot be replayed indefinitely.
INIT_DATA_MAX_AGE = config.get_int("WEBAPP_INITDATA_MAX_AGE", 86400)
# Auth is mandatory as soon as the app is reachable from outside this machine.
REQUIRE_AUTH = config.get_bool("WEBAPP_REQUIRE_AUTH", bool(config.WEBAPP_PUBLIC_URL))

MIME = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8", ".svg": "image/svg+xml",
        ".png": "image/png", ".ico": "image/x-icon"}


# --------------------------------------------------------------------------
# Telegram WebApp initData verification
# --------------------------------------------------------------------------

def verify_init_data(init_data: str) -> dict | None:
    """Validate Telegram WebApp initData. Returns the parsed user dict, or None.

    Algorithm per Telegram docs: secret = HMAC_SHA256("WebAppData", bot_token),
    then the hash must equal HMAC_SHA256(secret, sorted "key=value" lines).
    """
    if not init_data or not config.TELEGRAM_BOT_TOKEN:
        return None
    try:
        pairs = urllib.parse.parse_qsl(init_data, keep_blank_values=True)
    except ValueError:
        return None
    received = dict(pairs)
    their_hash = received.pop("hash", "")
    if not their_hash:
        return None
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(received.items()))
    secret = hmac.new(b"WebAppData", config.TELEGRAM_BOT_TOKEN.encode(), hashlib.sha256).digest()
    ours = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(ours, their_hash):
        return None
    try:
        auth_date = int(received.get("auth_date", "0"))
    except ValueError:
        return None
    if INIT_DATA_MAX_AGE > 0 and time.time() - auth_date > INIT_DATA_MAX_AGE:
        return None
    try:
        user = json.loads(received.get("user", "{}"))
    except json.JSONDecodeError:
        return None
    # The panel belongs to exactly one operator.
    if config.TELEGRAM_CHAT_ID and str(user.get("id", "")) != str(config.TELEGRAM_CHAT_ID):
        return None
    return user


# --------------------------------------------------------------------------
# Live price cache for open positions
# --------------------------------------------------------------------------

_prices: dict[str, float] = {}
_prices_at: float = 0.0
_prices_lock = threading.Lock()


def _price_refresher(stop: threading.Event) -> None:
    """Keep marks for open positions warm so API responses stay fast."""
    global _prices_at
    import paper_engine as pe

    while not stop.is_set():
        try:
            c = sqlite3.connect(config.DB_PATH, timeout=30)
            rows = c.execute("SELECT token_mint,chain FROM paper_positions WHERE status='open'").fetchall()
            c.close()
            fresh = {}
            for mint, chain in rows:
                try:
                    p = pe.token_price(chain, mint)
                except Exception as e:
                    LOG.warning("price %s: %s", mint[:8], e)
                    p = 0.0
                if p > 0:
                    fresh[mint] = p
            with _prices_lock:
                _prices.update(fresh)
                _prices_at = time.time()
        except Exception as e:
            LOG.warning("price refresher: %s", e)
        stop.wait(config.PRICE_TTL)


def mark_price(mint: str) -> float:
    with _prices_lock:
        return _prices.get(mint, 0.0)


# --------------------------------------------------------------------------
# Data access
# --------------------------------------------------------------------------

def db() -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True, timeout=15)
    c.row_factory = sqlite3.Row
    return c


def _positions(c: sqlite3.Connection) -> list[dict]:
    out = []
    for r in c.execute(
        "SELECT token_mint,chain,entry_price,peak_price,stake_sol,opened_at,signal_score,wallet_count "
        "FROM paper_positions WHERE status='open' ORDER BY opened_at DESC"
    ):
        current = mark_price(r["token_mint"]) or r["peak_price"]
        change = (current / r["entry_price"] - 1) if r["entry_price"] else 0.0
        peak = max(r["peak_price"], current)
        peak_gain = (peak / r["entry_price"] - 1) if r["entry_price"] else 0.0
        trail_armed = peak_gain >= config.TRAILING_ACTIVATE_PCT / 100
        out.append({
            "mint": r["token_mint"], "chain": r["chain"],
            "entry_price": r["entry_price"], "peak_price": peak, "current_price": current,
            "stake_sol": r["stake_sol"], "opened_at": r["opened_at"],
            "age_seconds": int(time.time()) - r["opened_at"],
            "score": r["signal_score"], "wallets": r["wallet_count"],
            "change_pct": change * 100, "pnl_sol": r["stake_sol"] * change,
            "trailing_armed": trail_armed,
            # Where the position gets closed right now, whichever stop binds first.
            "stop_price": max(
                r["entry_price"] * (1 - config.HARD_STOP_PCT / 100),
                peak * (1 - config.TRAILING_DISTANCE_PCT / 100) if trail_armed else 0.0,
            ),
            "expires_in": max(0, config.MAX_HOLD_SECONDS - (int(time.time()) - r["opened_at"])),
            "priced": mark_price(r["token_mint"]) > 0,
        })
    return out


def api_overview() -> dict:
    c = db()
    try:
        a = c.execute("SELECT budget_sol,initial_budget_sol,bankrupt,updated_at FROM paper_account WHERE id=1").fetchone()
        positions = _positions(c)
        open_value = sum(p["stake_sol"] + p["pnl_sol"] for p in positions)
        equity = (a["budget_sol"] if a else 0.0) + open_value
        initial = a["initial_budget_sol"] if a else 0.0

        closed = c.execute(
            "SELECT pnl_sol,pnl_pct FROM paper_trades WHERE action='EXIT'"
        ).fetchall()
        wins = [r for r in closed if r["pnl_sol"] > 0]
        realized = sum(r["pnl_sol"] for r in closed)
        best = max((r["pnl_pct"] for r in closed), default=0.0)
        worst = min((r["pnl_pct"] for r in closed), default=0.0)

        wallets = c.execute(
            "SELECT COUNT(*) n, SUM(winrate>=0.70) elite, SUM(winrate>=0.50) active, "
            "AVG(CASE WHEN winrate>0 THEN winrate END) avg FROM wallet_watch WHERE active=1"
        ).fetchone()

        return {
            "balance_sol": a["budget_sol"] if a else 0.0,
            "initial_sol": initial,
            "equity_sol": equity,
            "open_value_sol": open_value,
            "total_pnl_sol": equity - initial,
            "total_pnl_pct": ((equity / initial - 1) * 100) if initial else 0.0,
            "realized_pnl_sol": realized,
            "bankrupt": bool(a["bankrupt"]) if a else False,
            "can_open": (a["budget_sol"] if a else 0.0) >= config.STAKE_SOL,
            "open_positions": len(positions),
            "positions": positions,
            "closed_trades": len(closed),
            "wins": len(wins),
            "losses": len(closed) - len(wins),
            "winrate_pct": (len(wins) / len(closed) * 100) if closed else 0.0,
            "best_pct": best * 100,
            "worst_pct": worst * 100,
            "wallets_total": wallets["n"] or 0,
            "wallets_elite": wallets["elite"] or 0,
            "wallets_active": wallets["active"] or 0,
            "wallets_avg_winrate_pct": (wallets["avg"] or 0) * 100,
            "prices_age": int(time.time() - _prices_at) if _prices_at else None,
            "engine_alive": engine_alive(c),
            "engine_last_cycle": engine_heartbeat(c),
            "config": {
                "entry_score": config.ENTRY_SCORE, "stake_sol": config.STAKE_SOL,
                "hard_stop_pct": config.HARD_STOP_PCT,
                "trailing_activate_pct": config.TRAILING_ACTIVATE_PCT,
                "trailing_distance_pct": config.TRAILING_DISTANCE_PCT,
                "max_hold_hours": config.MAX_HOLD_SECONDS / 3600,
                "chains": config.CHAINS, "poll_seconds": config.POLL_SECONDS,
            },
            "server_time": int(time.time()),
        }
    finally:
        c.close()


def engine_heartbeat(c: sqlite3.Connection) -> int:
    """Unix ts of the engine's last completed cycle, or 0 if it has never run."""
    try:
        row = c.execute("SELECT updated_at FROM engine_state WHERE key='last_cycle'").fetchone()
        if row:
            return row[0]
    except sqlite3.OperationalError:
        pass  # pre-heartbeat database
    row = c.execute("SELECT MAX(event_ts) FROM engine_events").fetchone()
    return (row[0] if row and row[0] else 0) or 0


def engine_alive(c: sqlite3.Connection) -> bool:
    last = engine_heartbeat(c)
    return bool(last and time.time() - last < max(120, config.POLL_SECONDS * 6))


def api_trades(limit: int = 50) -> dict:
    c = db()
    try:
        rows = c.execute(
            "SELECT id,token_mint,chain,action,price,stake_sol,pnl_sol,pnl_pct,reason,"
            "wallet_count,signal_score,event_ts FROM paper_trades ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return {"trades": [dict(r) for r in rows]}
    finally:
        c.close()


def api_wallets(limit: int = 100) -> dict:
    c = db()
    try:
        rows = c.execute(
            "SELECT address,chain,source,winrate,last_seen,updated_at FROM wallet_watch "
            "WHERE active=1 AND winrate>0 ORDER BY winrate DESC, last_seen DESC LIMIT ?",
            (limit,),
        ).fetchall()
        buckets = c.execute(
            "SELECT SUM(winrate>=0.90) w90, SUM(winrate>=0.70 AND winrate<0.90) w70, "
            "SUM(winrate>=0.60 AND winrate<0.70) w60, SUM(winrate>=0.50 AND winrate<0.60) w50, "
            "SUM(winrate=0) unknown FROM wallet_watch WHERE active=1"
        ).fetchone()
        blacklisted = c.execute("SELECT COUNT(*) FROM wallet_blacklist").fetchone()[0]
        return {
            "wallets": [dict(r) for r in rows],
            "buckets": {k: (buckets[k] or 0) for k in ("w90", "w70", "w60", "w50", "unknown")},
            "blacklisted": blacklisted,
        }
    finally:
        c.close()


def api_weights(limit: int = 30) -> dict:
    c = db()
    try:
        rows = c.execute(
            "SELECT chain,token_mint,score,buy_wallets,total_wallets,updated_at FROM token_scores "
            "WHERE score>0 ORDER BY score DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return {
            "entry_score": config.ENTRY_SCORE,
            "weights": [dict(r) | {"need": max(0.0, config.ENTRY_SCORE - r["score"])} for r in rows],
        }
    finally:
        c.close()


def api_events(limit: int = 60) -> dict:
    c = db()
    try:
        rows = c.execute(
            "SELECT id,event_ts,kind,message FROM engine_events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return {"events": [dict(r) for r in rows]}
    finally:
        c.close()


def api_equity_curve(limit: int = 200) -> dict:
    """Reconstruct the equity curve from closed trades, oldest first."""
    c = db()
    try:
        a = c.execute("SELECT initial_budget_sol FROM paper_account WHERE id=1").fetchone()
        equity = a["initial_budget_sol"] if a else 0.0
        rows = c.execute(
            "SELECT pnl_sol,event_ts FROM paper_trades WHERE action='EXIT' ORDER BY id LIMIT ?", (limit,)
        ).fetchall()
        points = [{"ts": rows[0]["event_ts"] if rows else int(time.time()), "equity": equity}]
        for r in rows:
            equity += r["pnl_sol"]
            points.append({"ts": r["event_ts"], "equity": equity})
        return {"curve": points}
    finally:
        c.close()


ROUTES = {
    "/api/overview": lambda q: api_overview(),
    "/api/trades": lambda q: api_trades(_limit(q, 50, 200)),
    "/api/wallets": lambda q: api_wallets(_limit(q, 100, 500)),
    "/api/weights": lambda q: api_weights(_limit(q, 30, 100)),
    "/api/events": lambda q: api_events(_limit(q, 60, 300)),
    "/api/equity": lambda q: api_equity_curve(_limit(q, 200, 1000)),
    "/api/health": lambda q: {"ok": True, "time": int(time.time())},
}


def _limit(q: dict, default: int, cap: int) -> int:
    try:
        return max(1, min(cap, int(q.get("limit", [default])[0])))
    except (TypeError, ValueError):
        return default


class Handler(BaseHTTPRequestHandler):
    server_version = "sentinel-webapp"

    def log_message(self, fmt, *args):  # quieter than the stdlib default
        LOG.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, payload: dict) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _authorized(self) -> bool:
        if not REQUIRE_AUTH:
            return True
        init_data = self.headers.get("X-Telegram-Init-Data", "")
        return verify_init_data(init_data) is not None

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = urllib.parse.parse_qs(parsed.query)

        if path.startswith("/api/"):
            handler = ROUTES.get(path)
            if handler is None:
                return self._json(404, {"error": "unknown endpoint"})
            if path != "/api/health" and not self._authorized():
                return self._json(401, {"error": "unauthorized: valid Telegram initData required"})
            try:
                return self._json(200, handler(query))
            except sqlite3.OperationalError as e:
                LOG.warning("db: %s", e)
                return self._json(503, {"error": f"database unavailable: {e}"})
            except Exception as e:
                LOG.exception("api %s", path)
                return self._json(500, {"error": str(e)})

        # Static files, confined to STATIC_DIR.
        rel = "index.html" if path == "/" else path.lstrip("/")
        target = (STATIC_DIR / rel).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
            return self._send(404, b"not found", "text/plain; charset=utf-8")
        ctype = MIME.get(target.suffix, "application/octet-stream")
        self._send(200, target.read_bytes(), ctype)


def serve(host: str | None = None, port: int | None = None, block: bool = True):
    host = host or config.WEBAPP_HOST
    port = port or config.WEBAPP_PORT
    stop = threading.Event()
    threading.Thread(target=_price_refresher, args=(stop,), daemon=True, name="prices").start()
    httpd = ThreadingHTTPServer((host, port), Handler)
    LOG.info("mini app on http://%s:%d (auth %s)", host, port, "required" if REQUIRE_AUTH else "off — local only")
    if config.WEBAPP_PUBLIC_URL:
        LOG.info("public origin: %s", config.WEBAPP_PUBLIC_URL)
    if not block:
        threading.Thread(target=httpd.serve_forever, daemon=True, name="httpd").start()
        return httpd, stop
    try:
        httpd.serve_forever()
    finally:
        stop.set()
        httpd.server_close()
    return httpd, stop


def main():
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=config.WEBAPP_HOST)
    ap.add_argument("--port", type=int, default=config.WEBAPP_PORT)
    args = ap.parse_args()
    config.use_utf8_stdio()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    serve(args.host, args.port)


if __name__ == "__main__":
    main()
