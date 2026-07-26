#!/usr/bin/env python3
"""Telegram bot for Sentinel: text commands, event push, and the Mini App button.

Stdlib only — no bot framework, no private key, no write access to the engine.
Credentials come from the repo-local .env through config.py.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402

LOG = logging.getLogger("telegram-bot")
DB = config.DB_PATH
TOKEN = config.TELEGRAM_BOT_TOKEN
CHAT = config.TELEGRAM_CHAT_ID
# Only an https origin can be opened as a Mini App; a bare localhost URL is rejected
# by Telegram, so the button is offered only when a public origin is configured.
WEBAPP_URL = config.WEBAPP_PUBLIC_URL if config.WEBAPP_PUBLIC_URL.startswith("https://") else ""

PUSH_KINDS = ("ENTRY", "EXIT", "WALLET", "WALLET_BUY", "BANKRUPT", "RECOVERY", "ERROR")


def api(method: str, data: dict | None = None) -> dict:
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    body = urllib.parse.urlencode(data or {}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=35) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:200]
        raise RuntimeError(f"{e.code} {detail}") from None


def keyboard() -> str:
    """Persistent reply keyboard; the Mini App gets its own row when reachable."""
    rows = [["/status", "/positions"], ["/trades", "/wallets"], ["/weights", "/help"]]
    kb = {"keyboard": rows, "resize_keyboard": True, "is_persistent": True}
    if WEBAPP_URL:
        kb["keyboard"] = [[{"text": "📊 Панель", "web_app": {"url": WEBAPP_URL}}]] + rows
    return json.dumps(kb)


def install_menu_button() -> None:
    """Attach the Mini App to the chat's menu button, so it opens from the ☰ icon."""
    if not WEBAPP_URL:
        LOG.info("WEBAPP_PUBLIC_URL not set (or not https) — Mini App button disabled")
        return
    try:
        api("setChatMenuButton", {
            "chat_id": CHAT,
            "menu_button": json.dumps({"type": "web_app", "text": "Панель",
                                       "web_app": {"url": WEBAPP_URL}}),
        })
        LOG.info("Mini App button installed: %s", WEBAPP_URL)
    except Exception as e:
        LOG.warning("could not install menu button: %s", e)


def install_commands() -> None:
    cmds = [
        ("status", "Счёт и открытые позиции"),
        ("positions", "Подробно по каждой позиции"),
        ("trades", "Последние сделки"),
        ("wallets", "Пул кошельков"),
        ("weights", "Монеты у порога входа"),
        ("config", "Параметры движка"),
        ("help", "Список команд"),
    ]
    try:
        api("setMyCommands", {"commands": json.dumps([{"command": c, "description": d} for c, d in cmds])})
    except Exception as e:
        LOG.warning("could not register commands: %s", e)


_last_event = 0


def push_events(c: sqlite3.Connection) -> None:
    global _last_event
    rows = c.execute(
        "SELECT id,kind,message FROM engine_events WHERE id>? ORDER BY id LIMIT 20", (_last_event,)
    ).fetchall()
    for eid, kind, msg in rows:
        _last_event = eid
        if kind not in PUSH_KINDS:
            continue
        icon = {"ENTRY": "🟢", "EXIT": "🔴", "WALLET": "👛", "WALLET_BUY": "⭐",
                "BANKRUPT": "💀", "RECOVERY": "♻️", "ERROR": "⚠️"}.get(kind, "•")
        try:
            api("sendMessage", {"chat_id": CHAT, "text": f"{icon} {kind}: {msg}"})
            time.sleep(0.35)
        except Exception as e:
            LOG.warning("push failed: %s", e)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def _fmt_price(p: float) -> str:
    if not p:
        return "—"
    return f"{p:.3e}" if p < 1e-3 else f"{p:.6g}"


def text(c: sqlite3.Connection, command: str) -> str:
    a = c.execute("SELECT budget_sol,initial_budget_sol,bankrupt FROM paper_account WHERE id=1").fetchone()
    if not a:
        return "База ещё не инициализирована — запусти движок."
    balance, initial, bankrupt = a

    if command in ("/status", "/start"):
        pos = c.execute(
            "SELECT token_mint,chain,stake_sol,entry_price,peak_price,opened_at,signal_score "
            "FROM paper_positions WHERE status='open' ORDER BY opened_at DESC"
        ).fetchall()
        realized = c.execute("SELECT COALESCE(SUM(pnl_sol),0) FROM paper_trades WHERE action='EXIT'").fetchone()[0]
        hb = c.execute("SELECT updated_at FROM engine_state WHERE key='last_cycle'").fetchone()
        age = int(time.time()) - hb[0] if hb else None
        alive = age is not None and age < max(120, config.POLL_SECONDS * 6)
        out = [
            f"{'💀 БАНКРОТ' if bankrupt else '🟢 LIVE' if alive else '🔴 ДВИЖОК СТОИТ'}",
            f"Баланс: {balance:.5f} SOL  (старт {initial:.5f})",
            f"Реализовано: {realized:+.5f} SOL",
            f"Открыто позиций: {len(pos)}",
        ]
        if age is not None:
            out.append(f"Последний цикл: {age} с назад")
        for m, chain, stake, entry, peak, opened, score in pos:
            held = (int(time.time()) - opened) // 60
            out.append(f"  {chain} {m[:8]}… стейк {stake:.4f} · вход {_fmt_price(entry)} · {held} мин · score {score:.3f}")
        if WEBAPP_URL:
            out.append("\nПодробности — кнопка «📊 Панель».")
        return "\n".join(out)

    if command == "/positions":
        pos = c.execute(
            "SELECT token_mint,chain,entry_price,peak_price,stake_sol,opened_at,signal_score,wallet_count "
            "FROM paper_positions WHERE status='open' ORDER BY opened_at DESC"
        ).fetchall()
        if not pos:
            return "Открытых позиций нет."
        out = []
        for m, chain, entry, peak, stake, opened, score, wc in pos:
            gain = (peak / entry - 1) * 100 if entry else 0
            hard = entry * (1 - config.HARD_STOP_PCT / 100)
            trail = peak * (1 - config.TRAILING_DISTANCE_PCT / 100) if gain >= config.TRAILING_ACTIVATE_PCT else 0
            left = max(0, config.MAX_HOLD_SECONDS - (int(time.time()) - opened)) // 60
            out.append(
                f"{chain} {m[:10]}…\n"
                f"  вход {_fmt_price(entry)} · пик {_fmt_price(peak)} ({gain:+.1f}%)\n"
                f"  стоп {_fmt_price(max(hard, trail))}{' (трейлинг)' if trail > hard else ''}\n"
                f"  стейк {stake:.4f} SOL · {wc} кош. · score {score:.3f} · закроется через {left} мин"
            )
        return "\n\n".join(out)

    if command == "/trades":
        rs = c.execute(
            "SELECT chain,token_mint,action,pnl_pct,pnl_sol,reason,event_ts FROM paper_trades "
            "ORDER BY id DESC LIMIT 12"
        ).fetchall()
        if not rs:
            return "Сделок пока нет."
        lines = []
        for chain, m, action, pnl_pct, pnl_sol, reason, ts in rs:
            when = time.strftime("%d.%m %H:%M", time.localtime(ts))
            if action == "EXIT":
                lines.append(f"{'🟩' if pnl_sol > 0 else '🟥'} {when} {m[:8]}… {pnl_pct*100:+.2f}% {pnl_sol:+.5f} SOL — {reason}")
            else:
                lines.append(f"🟦 {when} ВХОД {m[:8]}… — {reason}")
        return "\n".join(lines)

    if command == "/wallets":
        rs = c.execute(
            "SELECT chain,COUNT(*),AVG(CASE WHEN winrate>0 THEN winrate END),"
            "SUM(winrate>=0.90),SUM(winrate>=0.70),SUM(winrate>=0.50) "
            "FROM wallet_watch WHERE active=1 GROUP BY chain"
        ).fetchall()
        if not rs:
            return "Кошельки ещё не загружены."
        black = c.execute("SELECT COUNT(*) FROM wallet_blacklist").fetchone()[0]
        out = [f"{r[0]}: всего {r[1]} · 90%+ {r[3]} · 70%+ {r[4]} · 50%+ {r[5]} · средний {(r[2] or 0)*100:.1f}%" for r in rs]
        out.append(f"В чёрном списке: {black}")
        return "\n".join(out)

    if command == "/weights":
        rs = c.execute(
            "SELECT chain,token_mint,score,buy_wallets,total_wallets FROM token_scores "
            "WHERE score>0 ORDER BY score DESC LIMIT 10"
        ).fetchall()
        if not rs:
            return "Нет монет с ненулевым весом — движок ещё не собрал кластер."
        head = f"Порог входа: {config.ENTRY_SCORE}\n"
        return head + "\n".join(
            f"{i+1}. {r[1][:10]}… score {r[2]:.4f} (не хватает {max(0, config.ENTRY_SCORE - r[2]):.4f}) · {r[3]}/{r[4]} кош."
            for i, r in enumerate(rs))

    if command == "/config":
        return (
            f"Сети: {', '.join(config.CHAINS)}\n"
            f"Опрос: {config.POLL_SECONDS} с\n"
            f"Порог входа: {config.ENTRY_SCORE}\n"
            f"Ставка: {config.STAKE_SOL} SOL\n"
            f"Жёсткий стоп: −{config.HARD_STOP_PCT}%\n"
            f"Трейлинг: от +{config.TRAILING_ACTIVATE_PCT}%, дистанция {config.TRAILING_DISTANCE_PCT}%\n"
            f"Макс. удержание: {config.MAX_HOLD_SECONDS//3600} ч\n"
            f"Окно кластера: {config.CLUSTER_WINDOW//60} мин"
        )

    return ("/status — счёт и позиции\n"
            "/positions — подробно по позициям\n"
            "/trades — последние сделки\n"
            "/wallets — пул кошельков\n"
            "/weights — монеты у порога входа\n"
            "/config — параметры движка\n"
            "/help — это меню")


def main():
    global _last_event
    config.use_utf8_stdio()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required — put it in .env (see .env.example)")
    if not CHAT:
        raise SystemExit("TELEGRAM_CHAT_ID is required — the bot must answer only its owner chat")

    c = sqlite3.connect(DB, check_same_thread=False, timeout=30)
    _last_event = c.execute("SELECT COALESCE(MAX(id),0) FROM engine_events").fetchone()[0]
    install_commands()
    install_menu_button()
    LOG.info("bot polling; pushing events to chat %s", CHAT)

    offset = 0
    while True:
        try:
            for u in api("getUpdates", {"timeout": 25, "offset": offset}).get("result", []):
                offset = u["update_id"] + 1
                msg = u.get("message", {})
                chat = str(msg.get("chat", {}).get("id", ""))
                parts = (msg.get("text") or "").split()
                if not parts or chat != CHAT:
                    continue
                api("sendMessage", {"chat_id": chat, "text": text(c, parts[0].split("@")[0]),
                                    "reply_markup": keyboard()})
        except Exception as e:
            if "409" in str(e):
                # Another poller grabbed the same token — drop the backlog and resync.
                api("getUpdates", {"offset": -1})
                offset = 0
            else:
                LOG.warning("telegram: %s", e)
            time.sleep(5)
        try:
            push_events(c)
        except Exception as e:
            LOG.warning("push loop: %s", e)


if __name__ == "__main__":
    main()
