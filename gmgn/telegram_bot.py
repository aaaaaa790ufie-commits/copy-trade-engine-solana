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
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
import paper_engine as pe  # noqa: E402
import webapp  # noqa: E402  (shared metric computation, so the two surfaces agree)

LOG = logging.getLogger("telegram-bot")
DB = config.DB_PATH
TOKEN = config.TELEGRAM_BOT_TOKEN
CHAT = config.TELEGRAM_CHAT_ID
# Only an https origin can be opened as a Mini App; a bare localhost URL is rejected
# by Telegram, so the button is offered only when a public origin is configured.
WEBAPP_URL = config.WEBAPP_PUBLIC_URL if config.WEBAPP_PUBLIC_URL.startswith("https://") else ""

PUSH_KINDS = ("ENTRY", "EXIT", "WALLET", "MISSED", "BANKRUPT", "RECOVERY", "ERROR", "STUCK", "DEPOSIT")

# Kinds worth seeing, but not every time. Wallet bookkeeping fires whenever the pool
# grows by even one address: 60 messages in a day against 4 entries and 4 exits, each
# reading "+1 новых, всего 1275". That buries the eight that matter under a 7:1 ratio of
# ones that do not. Throttled at push time rather than at emit, so the journal, the
# panel's feed and /wallets stay complete — only the notification is thinned, and the
# message already carries the running total, so the one that does go out says everything
# the seven it replaces did. Set to 0 to push every one.
WALLET_PUSH_INTERVAL = config.get_int("TELEGRAM_WALLET_PUSH_SECONDS", 3600)
PUSH_INTERVALS = {"WALLET": WALLET_PUSH_INTERVAL}
_last_pushed_at: dict[str, float] = {}


def due_for_push(kind: str, now: float) -> bool:
    """Whether this kind may be sent now, given its throttle. Pure — asking is free.

    Only throttled kinds are affected; ENTRY, EXIT, ERROR and the rest are never
    delayed, because those are the ones the operator is waiting for.

    Deliberately does not record anything. It used to, and that made a failed send
    lose its message: the send was marked as having happened, `push_events` returned
    without advancing the cursor, and on the retry pass the throttle suppressed the very
    event being retried — after which the cursor moved past it. `mark_pushed` is called
    only once the message is actually out.
    """
    interval = PUSH_INTERVALS.get(kind, 0)
    if interval <= 0:
        return True
    last = _last_pushed_at.get(kind)
    # None is "never sent", not "sent at the epoch". Defaulting to 0.0 and subtracting
    # gives the right answer against a real clock only because time.time() dwarfs any
    # interval — it would suppress the first message of every kind under any other
    # clock, which is a coincidence holding the behaviour up rather than a rule.
    return last is None or now - last >= interval


def mark_pushed(kind: str, now: float) -> None:
    """Record a message that actually reached Telegram."""
    if PUSH_INTERVALS.get(kind, 0) > 0:
        _last_pushed_at[kind] = now


def api(method: str, data: dict | None = None) -> dict:
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    body = urllib.parse.urlencode(data or {}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=35) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:200]
        raise RuntimeError(f"{e.code} {detail}") from None


# One list behind three surfaces: Telegram's command menu, the reply keyboard, and the
# /help text. They were three hand-maintained copies plus a fourth in CLAUDE.md, so a
# new command could reach the dispatcher and appear in none of them. This project has
# already paid for that pattern three times — the stop level, the liveness threshold and
# the weight ladder each existed in three places before they were made to derive.
#   (name, menu description, /help line)
COMMANDS = [
    ("status",      "Счёт и открытые позиции",    "счёт и позиции"),
    ("positions",   "Подробно по каждой позиции", "подробно по позициям"),
    ("trades",      "Последние сделки",           "последние сделки"),
    ("wallets",     "Пул кошельков",              "пул кошельков"),
    ("weights",     "Монеты у порога входа",      "монеты у порога входа"),
    ("attribution", "Кто заработал этому счёту",  "кто заработал этому счёту"),
    ("config",      "Параметры движка",           "параметры движка"),
    ("help",        "Список команд",              "это меню"),
]


def help_text() -> str:
    return "\n".join(f"/{name} — {line}" for name, _, line in COMMANDS)


def keyboard() -> str:
    """Persistent reply keyboard; the Mini App gets its own row when reachable."""
    names = [f"/{name}" for name, _, _ in COMMANDS]
    rows = [names[i:i + 2] for i in range(0, len(names), 2)]
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
    try:
        api("setMyCommands", {"commands": json.dumps(
            [{"command": name, "description": desc} for name, desc, _ in COMMANDS])})
    except Exception as e:
        LOG.warning("could not register commands: %s", e)


ICONS = {"ENTRY": "🟢", "EXIT": "🔴", "WALLET": "👛", "MISSED": "⚪",
         "BANKRUPT": "💀", "RECOVERY": "♻️", "ERROR": "⚠️", "STUCK": "🧊", "DEPOSIT": "💰"}
# Events per push pass. The loop runs once per getUpdates round-trip, so this only
# throttles a burst; the remainder goes out on the next pass.
PUSH_BATCH = config.get_int("TELEGRAM_PUSH_BATCH", 20)
# Above this backlog the bot summarises instead of replaying every message, so a long
# outage does not produce hundreds of notifications.
CATCHUP_LIMIT = config.get_int("TELEGRAM_CATCHUP_LIMIT", 15)
# The bot's read cursor. Kept beside the database rather than inside it, so the bot
# needs no write access to the engine's data.
STATE_PATH = Path(config.get("TELEGRAM_BOT_STATE", str(Path(config.DB_PATH).parent / "bot_state.json")))

_last_event = 0


def load_cursor() -> int | None:
    """The persisted cursor, or None if there is no usable state file.

    None and 0 are different: 0 is a legitimate cursor on a database whose journal is
    still empty, and conflating them made every restart look like a first run.
    """
    try:
        return int(json.loads(STATE_PATH.read_text(encoding="utf-8"))["last_event"])
    except (OSError, ValueError, TypeError, KeyError):
        return None


def save_cursor(eid: int) -> None:
    """Persist the cursor atomically: write a sibling temp file, then rename over.

    A plain write_text can be interrupted, and the supervisor terminates this process
    routinely — on every tunnel URL change, which is hourly on a free pinggy session.
    A half-written file makes load_cursor() return None, catch_up() read that as a first
    run, and the whole pending backlog gets skipped, which is the exact outcome catch_up
    exists to prevent. os.replace is atomic on POSIX and on Windows.
    """
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=str(STATE_PATH.parent), prefix=".bot_state.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"last_event": eid}, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, STATE_PATH)
        tmp = None
    except OSError as e:
        LOG.warning("could not persist the event cursor to %s: %s", STATE_PATH, e)
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def catch_up(c: sqlite3.Connection) -> None:
    """Report what happened while the bot was down, instead of dropping it.

    The cursor used to be reset to MAX(id) on every start, so every event during a
    restart was silently discarded — including BANKRUPT, ERROR and EXIT, the ones most
    worth seeing. The supervisor now restarts the bot whenever the tunnel URL changes,
    which made that routine rather than rare.
    """
    global _last_event
    cursor = load_cursor()
    if cursor is None:
        # First ever run: start from now rather than replaying the whole journal.
        _last_event = c.execute("SELECT COALESCE(MAX(id),0) FROM engine_events").fetchone()[0]
        save_cursor(_last_event)
        return
    _last_event = cursor
    rows = c.execute(
        "SELECT kind,COUNT(*) FROM engine_events WHERE id>? AND kind IN (%s) GROUP BY kind"
        % ",".join("?" * len(PUSH_KINDS)),
        (_last_event, *PUSH_KINDS),
    ).fetchall()
    missed = sum(n for _, n in rows)
    if missed > CATCHUP_LIMIT:
        summary = ", ".join(f"{ICONS.get(k, '•')} {k}×{n}" for k, n in sorted(rows, key=lambda r: -r[1]))
        try:
            api("sendMessage", {"chat_id": CHAT, "text": f"⏳ Пропущено за время простоя: {missed}\n{summary}"})
        except Exception as e:
            LOG.warning("catch-up summary failed: %s", e)
        _last_event = c.execute("SELECT COALESCE(MAX(id),0) FROM engine_events").fetchone()[0]
        save_cursor(_last_event)
    elif missed:
        LOG.info("replaying %d missed events", missed)


def push_events(c: sqlite3.Connection) -> None:
    global _last_event
    rows = c.execute(
        "SELECT id,kind,message FROM engine_events WHERE id>? ORDER BY id LIMIT ?",
        (_last_event, PUSH_BATCH),
    ).fetchall()
    for eid, kind, msg in rows:
        if kind in PUSH_KINDS and due_for_push(kind, time.time()):
            try:
                api("sendMessage", {"chat_id": CHAT, "text": f"{ICONS.get(kind, '•')} {kind}: {msg}"})
                mark_pushed(kind, time.time())   # only once it is genuinely out
                time.sleep(0.35)
            except Exception as e:
                # Leave the cursor where it is so the event is retried, rather than
                # advancing past an event that was never delivered.
                LOG.warning("push failed, will retry: %s", e)
                return
        _last_event = eid
    if rows:
        save_cursor(_last_event)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def _plural(n: int, one: str, few: str, many: str) -> str:
    """Russian count agreement: 1 сделку, 2 сделки, 5 сделок."""
    tens, ones = n % 100, n % 10
    if 11 <= tens <= 14:
        word = many
    elif ones == 1:
        word = one
    elif 2 <= ones <= 4:
        word = few
    else:
        word = many
    return f"{n} {word}"


def _fmt_price(p: float) -> str:
    if not p:
        return "—"
    return f"{p:.3e}" if p < 1e-3 else f"{p:.6g}"


def reply(c: sqlite3.Connection, command: str) -> str:
    """text(), but a failure answers the user instead of vanishing.

    Anything raised here escapes to the poll loop, which logs and sleeps — and because
    the update offset has already advanced, the command is never retried. The user just
    sees silence. Every failure therefore has to come back as a message.
    """
    try:
        return text(c, command)
    except sqlite3.OperationalError as e:
        # Typically a fresh install where the engine has not created its tables yet.
        LOG.warning("query failed for %s: %s", command, e)
        return f"База ещё не готова ({e}). Запусти движок: python gmgn/run_engine.py"
    except Exception as e:
        LOG.exception("command %s failed", command)
        return f"Не смог собрать ответ на {command}: {type(e).__name__}: {e}"


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
        last_cycle = pe.last_cycle_ts(c)
        age = int(time.time()) - last_cycle if last_cycle else None
        alive = pe.engine_is_alive(last_cycle)

        # Every figure leads with what the settings in force have done, and carries the
        # lifetime number — which still includes whatever earlier configurations did — in
        # parentheses. Before any reset there is only one period, so nothing is bracketed.
        reset_at = pe.reset_ts(c)
        lifetime = webapp.performance(c, 0)
        current = webapp.performance(c, reset_at) if reset_at else lifetime

        def both(fmt) -> str:
            """Lifetime first, current period in parentheses."""
            life = fmt(lifetime)
            shown = fmt(current)
            return life + " (" + shown + ")" if reset_at else life

        def money(s):
            return "{:+.5f} SOL".format(s["realized_sol"])

        def counted(s):
            return str(s["closed"])

        def win_loss(s):
            return "{} / {}".format(s["wins"], s["losses"])

        def winrate(s):
            return "{:.0f}%".format(s["winrate_pct"]) if s["closed"] else "—"

        def best(s):
            return "{:+.2f}%".format(s["best_pct"]) if s["closed"] else "—"

        def worst(s):
            return "{:+.2f}%".format(s["worst_pct"]) if s["closed"] else "—"

        # Three states, not two: the loop can be cycling while the feed returns nothing,
        # in which case the engine cannot enter or price anything and saying LIVE would
        # be misleading.
        fresh = pe.feed_is_fresh(c)
        if bankrupt:
            state = "💀 БАНКРОТ"
        elif not alive:
            state = "🔴 ДВИЖОК СТОИТ"
        elif not fresh:
            state = "🟡 НЕТ ДАННЫХ ОТ GMGN"
        else:
            state = "🟢 LIVE"
        out = [
            state,
            f"Баланс: {balance:.5f} SOL",
        ]
        if reset_at:
            hours = (int(time.time()) - reset_at) / 3600
            out.append(f"Метрики за {hours:.1f} ч с пополнения (в скобках — тек.период):")
        out += [
            "P&L: " + both(money),
            "Сделок: " + both(counted),
            "W / L: " + both(win_loss),
            "Винрейт: " + both(winrate),
            "Лучшая: " + both(best),
            "Худшая: " + both(worst),
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
            # Shared with the engine, so this is the stop exits() will actually act on.
            stop, hard, trail, _ = pe.stop_level(entry, peak)
            left = max(0, config.MAX_HOLD_SECONDS - (int(time.time()) - opened)) // 60
            out.append(
                f"{chain} {m[:10]}…\n"
                f"  вход {_fmt_price(entry)} · пик {_fmt_price(peak)} ({gain:+.1f}%)\n"
                f"  стоп {_fmt_price(stop)}{' (трейлинг)' if trail > hard else ''}\n"
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
        total, avg = c.execute(
            "SELECT COUNT(*),AVG(CASE WHEN winrate>0 THEN winrate END) "
            "FROM wallet_watch WHERE active=1").fetchone()
        if not total:
            return "Кошельки ещё не загружены."
        # Bands come from the same function the panel uses. This command previously
        # queried two of its columns with the same threshold — ELITE_WINRATE and
        # WEIGHT_TIERS[0][0] are both 0.90 now — under labels reading "90%+" and "70%+",
        # so it reported the same number twice under different names.
        out = [f"Активных в пуле: {total} · средний winrate {(avg or 0)*100:.1f}%"]
        for band in webapp.winrate_bands(c):
            if not band["count"]:
                continue
            mark = " — входит один" if band["enters_alone"] else ""
            out.append(f"  {band['label']}: {band['count']} (вес {band['weight']}){mark}")
        parked = c.execute("SELECT COUNT(*) FROM wallet_watch WHERE active=0").fetchone()[0]
        black = c.execute("SELECT COUNT(*) FROM wallet_blacklist").fetchone()[0]
        out.append(f"На паузе (мало сделок / не покупают): {parked}")
        out.append(f"В чёрном списке: {black}")
        return "\n".join(out)

    if command == "/weights":
        rs = c.execute(
            "SELECT chain,token_mint,score,buy_wallets,total_wallets FROM token_scores "
            "WHERE score>0 ORDER BY score DESC LIMIT 10"
        ).fetchall()
        # "LIVE with no positions" is ambiguous — a quiet market and a threshold that is
        # out of reach look identical from outside. This is the number that separates them.
        s = pe.signal_summary(c)
        head_lines = []
        if s["cycles"]:
            head_lines.append(
                f"За {s['window_hours']}ч: лучший сигнал {s['best_score']:.4f} из "
                f"{s['entry_score']}, порог взят "
                f"{_plural(s['cycles_at_threshold'], 'раз', 'раза', 'раз')} "
                f"за {s['cycles']} циклов")
        if not rs:
            return "\n".join(head_lines + ["Нет монет с ненулевым весом — движок ещё не собрал кластер."])
        head = "\n".join(head_lines + [f"Порог входа: {config.ENTRY_SCORE}"]) + "\n"
        return head + "\n".join(
            f"{i+1}. {r[1][:10]}… score {r[2]:.4f} (не хватает {max(0, config.ENTRY_SCORE - r[2]):.4f}) · {r[3]}/{r[4]} кош."
            for i, r in enumerate(rs))

    if command == "/attribution":
        rows = pe.wallet_attribution(c, limit=15)
        if not rows:
            return ("Пока не по чему считать — нужна хотя бы одна закрытая сделка.\n"
                    "Атрибуция делит результат сделки между кошельками, чьи покупки её вызвали.")
        out = ["Кто заработал этому счёту (доля по вкладу в сигнал):"]
        for r in rows:
            mark = "🟩" if r["attributed_sol"] > 0 else "🟥" if r["attributed_sol"] < 0 else "⬜"
            out.append(f"{mark} {r['address'][:10]}… wr {r['winrate']*100:.0f}% · "
                       f"{r['wins']}/{r['trades']} · {r['attributed_sol']:+.5f} SOL")
        total = sum(r["attributed_sol"] for r in rows)
        out.append(f"\nИтого по показанным: {total:+.5f} SOL")
        return "\n".join(out)

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

    # Also the fallback for anything unrecognised, which is why it is not dispatched
    # by name above.
    return help_text()


def main():
    global _last_event
    config.use_utf8_stdio()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required — put it in .env (see .env.example)")
    if not CHAT:
        raise SystemExit("TELEGRAM_CHAT_ID is required — the bot must answer only its owner chat")

    if not Path(DB).is_file():
        raise SystemExit(f"database not found at {DB} — start the engine first (python gmgn/run_engine.py)")
    # Read-only: the bot reports, it never writes to the engine's data. A plain
    # connect() would also create an empty database if the path were wrong, turning a
    # typo into a bot that starts cleanly and then fails every query.
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, check_same_thread=False, timeout=30)
    catch_up(c)
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
                try:
                    api("sendMessage", {"chat_id": chat, "text": reply(c, parts[0].split("@")[0]),
                                        "reply_markup": keyboard()})
                except Exception as e:
                    # One undeliverable reply must not abandon the updates behind it;
                    # their offsets are already consumed, so they would be lost.
                    LOG.warning("could not answer %s: %s", parts[0], e)
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
