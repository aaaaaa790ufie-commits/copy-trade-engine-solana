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
import paper_engine as pe  # noqa: E402

LOG = logging.getLogger("webapp")
STATIC_DIR = Path(__file__).resolve().parent / "webapp"

# initData signatures older than this are rejected even if the HMAC is valid,
# so a captured URL cannot be replayed indefinitely.
INIT_DATA_MAX_AGE = config.get_int("WEBAPP_INITDATA_MAX_AGE", 86400)

LOOPBACK = ("127.0.0.1", "::1", "localhost")


def _reachable_from_outside() -> bool:
    """True when the panel is not confined to this machine.

    A public origin is the obvious case, but binding to 0.0.0.0 exposes it to the whole
    LAN just as effectively — and defaulting auth off there would serve the account and
    wallet data to anyone on the network.
    """
    return bool(config.WEBAPP_PUBLIC_URL) or config.WEBAPP_HOST not in LOOPBACK


# Auth is mandatory as soon as the app is reachable from outside this machine.
REQUIRE_AUTH = config.get_bool("WEBAPP_REQUIRE_AUTH", _reachable_from_outside())

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


def merge_marks(open_mints: list[str], fresh: dict[str, float], previous: dict[str, float]) -> dict[str, float]:
    """Marks for exactly the currently-open positions.

    Rebuilt rather than accumulated, so the cache cannot grow to hold a mark for every
    position ever opened. A mint the API failed on this pass keeps its previous mark
    instead of blanking — `prices_age` tells the client how stale the set is.
    """
    return {m: fresh.get(m) or previous.get(m, 0.0) for m in open_mints}


def _price_refresher(stop: threading.Event) -> None:
    """Keep marks for open positions warm so API responses stay fast."""
    global _prices, _prices_at
    import paper_engine as pe

    while not stop.is_set():
        try:
            # Read-only, and closed on the failure path too: without the finally, a
            # raising execute() leaked a connection on every pass of this loop.
            c = db()
            try:
                rows = c.execute("SELECT token_mint,chain FROM paper_positions WHERE status='open'").fetchall()
            finally:
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
            merged = merge_marks([m for m, _ in rows], fresh, _prices)
            with _prices_lock:
                # Rebind rather than clear-then-update: the latter leaves a window in
                # which a reader holding the lock sees an empty dict and every position
                # blinks to "unpriced".
                _prices = merged
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
        # Without a live mark the position is shown at cost, not at peak_price. peak is the
        # best price ever seen, so falling back to it reported the most flattering possible
        # P&L for exactly the positions we know least about — a rugged token would have
        # displayed ~0% while actually being near -100%. `priced` tells the UI to mark it.
        mark = mark_price(r["token_mint"])
        current = mark or r["entry_price"]
        change = (current / r["entry_price"] - 1) if r["entry_price"] else 0.0
        peak = max(r["peak_price"], current)
        # From paper_engine, so the stop shown here is by construction the stop exits()
        # enforces rather than a second implementation of the same rules.
        stop, _, _, trail_armed = pe.stop_level(r["entry_price"], peak)
        out.append({
            "mint": r["token_mint"], "chain": r["chain"],
            "entry_price": r["entry_price"], "peak_price": peak, "current_price": current,
            "stake_sol": r["stake_sol"], "opened_at": r["opened_at"],
            "age_seconds": int(time.time()) - r["opened_at"],
            "score": r["signal_score"], "wallets": r["wallet_count"],
            "change_pct": change * 100, "pnl_sol": r["stake_sol"] * change,
            "trailing_armed": trail_armed,
            "stop_price": stop,
            "expires_in": max(0, config.MAX_HOLD_SECONDS - (int(time.time()) - r["opened_at"])),
            "priced": mark > 0,
        })
    return out


def performance(c: sqlite3.Connection, since: int = 0) -> dict:
    """Trade statistics over exits strictly after `since`; `since=0` means lifetime.

    Every headline figure is computed here so the two periods cannot drift apart — the
    panel reports the current settings' numbers first and the lifetime ones beside them.
    """
    # Columns read positionally, not by name: the bot shares this function and its
    # connection has no row_factory, so name access raised TypeError there.
    rows = c.execute(
        "SELECT pnl_sol,pnl_pct FROM paper_trades WHERE action='EXIT' AND event_ts>?",
        (since,),
    ).fetchall()
    pnls = [r[0] for r in rows]
    pcts = [r[1] for r in rows]
    wins = sum(1 for p in pnls if p > 0)
    return {
        "closed": len(rows),
        "wins": wins,
        "losses": len(rows) - wins,
        "winrate_pct": (wins / len(rows) * 100) if rows else 0.0,
        "realized_sol": sum(pnls),
        "best_pct": max(pcts, default=0.0) * 100,
        "worst_pct": min(pcts, default=0.0) * 100,
    }


def api_overview() -> dict:
    c = db()
    try:
        a = c.execute("SELECT budget_sol,initial_budget_sol,bankrupt,updated_at FROM paper_account WHERE id=1").fetchone()
        positions = _positions(c)
        open_value = sum(p["stake_sol"] + p["pnl_sol"] for p in positions)
        equity = (a["budget_sol"] if a else 0.0) + open_value
        initial = a["initial_budget_sol"] if a else 0.0

        # Two periods, same shape. `current` is what the settings in force have done
        # since the last top-up; `lifetime` still carries whatever came before it.
        reset_at = pe.reset_ts(c)
        lifetime = performance(c, 0)
        current = performance(c, reset_at) if reset_at else lifetime
        # Balance the account was restored to. Derived rather than stored: the top-up
        # raises initial by the same amount, so initial + realised-before-reset is the
        # balance at that moment, exactly.
        realized_before = c.execute(
            "SELECT COALESCE(SUM(pnl_sol),0) FROM paper_trades WHERE action='EXIT' AND event_ts<=?",
            (reset_at,)).fetchone()[0] if reset_at else 0.0

        # `active=1` means eligible to carry weight. Wallets parked at active=0 (too small
        # a sample, or no recent buys) are counted separately rather than hidden, so the
        # panel shows why the pool is smaller than the row count suggests.
        wallets = c.execute(
            f"SELECT COUNT(*) n, SUM(winrate>={pe.WEIGHT_TIERS[0][0]}) elite, "
            f"SUM(winrate>={pe.MIN_WEIGHTED_WINRATE}) qualified, "
            "AVG(CASE WHEN winrate>0 THEN winrate END) avg FROM wallet_watch WHERE active=1"
        ).fetchone()
        parked = c.execute("SELECT COUNT(*) FROM wallet_watch WHERE active=0").fetchone()[0]

        return {
            "balance_sol": a["budget_sol"] if a else 0.0,
            "initial_sol": initial,
            "equity_sol": equity,
            "open_value_sol": open_value,
            "total_pnl_sol": equity - initial,
            # None, not 0.0, when there is no positive base to measure against. A
            # withdrawal larger than the capital ever contributed drives initial to zero
            # or below, and `equity / initial` then flips the sign — a +0.2 SOL gain
            # rendered as -133%. The SOL figure beside it stays correct either way, so
            # the honest answer is to show that alone rather than an inverted percentage.
            "total_pnl_pct": ((equity / initial - 1) * 100) if initial > 0 else None,
            "realized_pnl_sol": lifetime["realized_sol"],
            "reset_at": reset_at,
            "lifetime": lifetime,
            "current": current,
            # Unrealised P&L belongs to the current period: the reset closed everything,
            # so every open position was opened under these settings.
            "current_pnl_sol": current["realized_sol"] + sum(p["pnl_sol"] for p in positions),
            "current_base_sol": (initial + realized_before) if reset_at else initial,
            "bankrupt": bool(a["bankrupt"]) if a else False,
            "can_open": (a["budget_sol"] if a else 0.0) >= config.STAKE_SOL,
            "open_positions": len(positions),
            "positions": positions,
            # Kept flat for anything still reading the old shape; both periods are in
            # "lifetime" and "current" above.
            "closed_trades": lifetime["closed"],
            "wins": lifetime["wins"],
            "losses": lifetime["losses"],
            "winrate_pct": lifetime["winrate_pct"],
            "best_pct": lifetime["best_pct"],
            "worst_pct": lifetime["worst_pct"],
            "wallets_total": wallets["n"] or 0,
            "wallets_elite": wallets["elite"] or 0,
            "wallets_qualified": wallets["qualified"] or 0,
            "wallets_parked": parked,
            "wallets_avg_winrate_pct": (wallets["avg"] or 0) * 100,
            "prices_age": int(time.time() - _prices_at) if _prices_at else None,
            "engine_alive": engine_alive(c),
            "feed_fresh": pe.feed_is_fresh(c),
            "last_feed_ok": pe.last_feed_ts(c),
            "engine_last_cycle": engine_heartbeat(c),
            "config": {
                "entry_score": config.ENTRY_SCORE, "stake_sol": config.STAKE_SOL,
                "hard_stop_pct": config.HARD_STOP_PCT,
                "trailing_activate_pct": config.TRAILING_ACTIVATE_PCT,
                "trailing_distance_pct": config.TRAILING_DISTANCE_PCT,
                "max_hold_hours": config.MAX_HOLD_SECONDS / 3600,
                "chains": config.CHAINS, "poll_seconds": config.POLL_SECONDS,
                "elite_winrate": pe.ELITE_WINRATE,
                "min_weighted_winrate": pe.MIN_WEIGHTED_WINRATE,
            },
            "server_time": int(time.time()),
        }
    finally:
        c.close()


def engine_heartbeat(c: sqlite3.Connection) -> int:
    """Unix ts of the engine's last completed cycle, or 0 if it has never run."""
    return pe.last_cycle_ts(c)


def engine_alive(c: sqlite3.Connection) -> bool:
    # Shared with the bot, so both agree on what "running" means.
    return pe.engine_is_alive(engine_heartbeat(c))


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


def winrate_bands(c: sqlite3.Connection) -> list[dict]:
    """One row per weight tier, counted from the ladder itself.

    Built by iterating WEIGHT_TIERS rather than unpacking a fixed number of them. The
    previous version did `elite, high, mid = ...`, which was correct for the three tiers
    that existed when it was written and raised ValueError — a 500 on the wallets tab —
    the moment the ladder grew to five. Deriving the count is the point of deriving at all.
    """
    bands, upper = [], None  # tiers are ordered high to low, so upper starts open-ended
    for low, weight in pe.WEIGHT_TIERS:
        if upper is None:
            n = c.execute("SELECT COUNT(*) FROM wallet_watch WHERE active=1 AND winrate>=?",
                          (low,)).fetchone()[0]
            label = f"{low*100:.0f}%+"
        else:
            n = c.execute("SELECT COUNT(*) FROM wallet_watch WHERE active=1 AND winrate>=? AND winrate<?",
                          (low, upper)).fetchone()[0]
            label = f"{low*100:.0f}–{upper*100:.0f}%"
        bands.append({"label": label, "min_winrate": low, "weight": weight, "count": n,
                      "enters_alone": weight >= config.ENTRY_SCORE})
        upper = low
    unknown = c.execute("SELECT COUNT(*) FROM wallet_watch WHERE active=1 AND winrate=0").fetchone()[0]
    bands.append({"label": "не оценены", "min_winrate": 0.0, "weight": 0.0,
                  "count": unknown, "enters_alone": False})
    return bands


def api_wallets(limit: int = 100) -> dict:
    c = db()
    try:
        rows = c.execute(
            "SELECT address,chain,source,winrate,last_seen,updated_at FROM wallet_watch "
            "WHERE active=1 AND winrate>0 ORDER BY winrate DESC, last_seen DESC LIMIT ?",
            (limit,),
        ).fetchall()
        blacklisted = c.execute("SELECT COUNT(*) FROM wallet_blacklist").fetchone()[0]
        return {
            "wallets": [dict(r) for r in rows],
            "bands": winrate_bands(c),
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
            "signal": pe.signal_summary(c),
            "weights": [dict(r) | {"need": max(0.0, config.ENTRY_SCORE - r["score"])} for r in rows],
        }
    finally:
        c.close()


def api_attribution(limit: int = 20) -> dict:
    """Realised P&L split across the wallets whose buys triggered each entry."""
    c = db()
    try:
        rows = pe.wallet_attribution(c, limit=limit)
        return {"wallets": rows,
                "total_sol": sum(r["attributed_sol"] for r in rows)}
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
    """Equity after each of the most recent `limit` closed trades, oldest first.

    `ORDER BY id LIMIT ?` took the *oldest* N, so once the journal passed the limit the
    chart froze on early history and never showed a recent trade again. Taking the tail
    means the curve cannot start from initial_budget_sol, so the trades before the
    window are folded into the opening value.
    """
    c = db()
    try:
        a = c.execute("SELECT initial_budget_sol FROM paper_account WHERE id=1").fetchone()
        initial = a["initial_budget_sol"] if a else 0.0
        realised = c.execute(
            "SELECT COALESCE(SUM(pnl_sol),0) FROM paper_trades WHERE action='EXIT'").fetchone()[0]
        rows = c.execute(
            "SELECT pnl_sol,event_ts FROM paper_trades WHERE action='EXIT' ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()[::-1]
        if not rows:
            return {"curve": [{"ts": int(time.time()), "equity": initial}]}
        equity = initial + realised - sum(r["pnl_sol"] for r in rows)
        points = [{"ts": rows[0]["event_ts"], "equity": equity}]
        for r in rows:
            equity += r["pnl_sol"]
            points.append({"ts": r["event_ts"], "equity": equity})
        return {"curve": points, "truncated": len(rows) == limit}
    finally:
        c.close()


ROUTES = {
    "/api/overview": lambda q: api_overview(),
    "/api/trades": lambda q: api_trades(_limit(q, 50, 200)),
    "/api/wallets": lambda q: api_wallets(_limit(q, 100, 500)),
    "/api/weights": lambda q: api_weights(_limit(q, 30, 100)),
    "/api/events": lambda q: api_events(_limit(q, 60, 300)),
    "/api/attribution": lambda q: api_attribution(_limit(q, 20, 200)),
    "/api/equity": lambda q: api_equity_curve(_limit(q, 200, 1000)),
    "/api/health": lambda q: {"ok": True, "time": int(time.time())},
}


def _limit(q: dict, default: int, cap: int) -> int:
    try:
        return max(1, min(cap, int(q.get("limit", [default])[0])))
    except (TypeError, ValueError):
        return default


def resolve_static(path: str) -> Path | None:
    """Map a URL path to a file inside STATIC_DIR, or None.

    The containment test is Path.is_relative_to, not a string prefix: STATIC_DIR is
    ".../gmgn/webapp", and a prefix test also accepts ".../gmgn/webapp-anything".
    On Windows there is a second trap — `Path("a") / "C:/x"` is "C:/x", because pathlib
    lets an absolute operand replace the base — so a request for "/C:/…" must be
    rejected by containment rather than assumed to stay under the root.
    """
    rel = "index.html" if path == "/" else path.lstrip("/")
    if not rel:
        return None
    root = STATIC_DIR.resolve()
    try:
        target = (root / rel).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            return None
    except (OSError, ValueError):  # malformed path, name too long, bad drive
        return None
    return target


class Handler(BaseHTTPRequestHandler):
    server_version = "sentinel-webapp"
    protocol_version = "HTTP/1.1"
    # Without this a half-open connection pins a thread forever, and ThreadingHTTPServer
    # spawns one per connection with no cap.
    timeout = config.get_int("WEBAPP_REQUEST_TIMEOUT", 30)

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
                return self._json(503, {"error": "database unavailable"})
            except Exception:
                # The detail goes to the log, not to the response: over a public tunnel
                # an exception string can disclose filesystem paths.
                LOG.exception("api %s", path)
                return self._json(500, {"error": "internal error"})

        target = resolve_static(path)
        if target is None:
            return self._send(404, b"not found", "text/plain; charset=utf-8")
        ctype = MIME.get(target.suffix, "application/octet-stream")
        try:
            body = target.read_bytes()
        except OSError as e:
            LOG.warning("static %s: %s", target, e)
            return self._send(404, b"not found", "text/plain; charset=utf-8")
        self._send(200, body, ctype)


class _Server(ThreadingHTTPServer):
    """ThreadingHTTPServer that will not quietly share a port with another instance.

    On POSIX, SO_REUSEADDR only permits rebinding a port left in TIME_WAIT — an active
    listener still wins, so the stdlib default is safe and worth keeping for fast
    restarts. On Windows it means something else entirely: a second process can bind a
    port a live server is already listening on, both succeed, and the OS hands each
    incoming connection to whichever it likes.

    That is reachable from the documented workflow. README says the pieces can be run
    individually, so `python gmgn/webapp.py` alongside a running supervisor gives two
    servers on 8770 — and the standalone one computes REQUIRE_AUTH with no public URL in
    its environment, so it serves the account data unauthenticated. Roughly half the
    requests arriving down the tunnel would land on it.

    Failing to bind is the correct outcome, and the caller turns it into a clear message.
    """
    allow_reuse_address = os.name != "nt"


def serve(host: str | None = None, port: int | None = None, block: bool = True):
    # `or` treated an explicit 0 as "not supplied" and replaced it with WEBAPP_PORT.
    # port=0 is the standard "give me any free port" idiom, so a caller asking for one
    # silently got the production port instead — which, with the sharing described
    # above, meant binding on top of a running server rather than beside it.
    host = config.WEBAPP_HOST if host is None else host
    port = config.WEBAPP_PORT if port is None else port
    # --host is a CLI argument, so it can differ from the value REQUIRE_AUTH was
    # computed from at import. Serving the account and wallet data unauthenticated to
    # the whole LAN should take a deliberate act, not a forgotten flag.
    if host not in LOOPBACK and not REQUIRE_AUTH and not config.get("WEBAPP_REQUIRE_AUTH"):
        raise SystemExit(
            f"refusing to serve on {host} without authentication.\n"
            "Set WEBAPP_PUBLIC_URL (which enables initData checking), or set "
            "WEBAPP_REQUIRE_AUTH=0 explicitly if this network is trusted."
        )
    stop = threading.Event()
    try:
        httpd = _Server((host, port), Handler)
    except OSError as e:
        raise SystemExit(
            f"cannot bind {host}:{port} — {e}\n"
            "Another Sentinel webapp is probably already running (the supervisor starts "
            "one). Stop it first, or set WEBAPP_PORT to a free port."
        ) from None
    # Started only once the port is ours, so a refused bind does not leave a thread
    # polling the price API behind a process that is about to exit.
    threading.Thread(target=_price_refresher, args=(stop,), daemon=True, name="prices").start()
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
