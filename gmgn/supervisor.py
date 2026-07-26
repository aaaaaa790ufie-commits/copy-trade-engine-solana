#!/usr/bin/env python3
"""Run engine + Telegram bot + Mini App server together, and keep them running.

A stopped engine is the expensive failure mode here: positions stay open past their
stop-loss until something checks the price again. The supervisor restarts any child
that exits, with exponential backoff so a permanently broken component does not spin.

    python gmgn/supervisor.py                 # all three
    python gmgn/supervisor.py --no-bot        # engine + mini app only
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402

HERE = Path(__file__).resolve().parent
LOG = logging.getLogger("supervisor")

MIN_BACKOFF = 3
MAX_BACKOFF = 120
# A child that survives this long is considered healthy, so its backoff resets.
HEALTHY_AFTER = 60


class Child:
    def __init__(self, name: str, argv: list[str]):
        self.name = name
        self.argv = argv
        self.proc: subprocess.Popen | None = None
        self.backoff = MIN_BACKOFF
        self.restarts = 0

    def start(self) -> None:
        LOG.info("starting %s", self.name)
        env = config.gmgn_env()
        # Children log Russian text and emoji; without this they die on a cp1251 console.
        env["PYTHONIOENCODING"] = "utf-8"
        self.proc = subprocess.Popen(
            self.argv, cwd=str(HERE.parent),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1, env=env,
        )
        threading.Thread(target=self._pump, daemon=True, name=f"log-{self.name}").start()

    def _pump(self) -> None:
        proc = self.proc
        if not proc or not proc.stdout:
            return
        for line in proc.stdout:
            print(f"[{self.name}] {line.rstrip()}", flush=True)

    def stop(self) -> None:
        if not self.proc or self.proc.poll() is not None:
            return
        LOG.info("stopping %s", self.name)
        try:
            self.proc.terminate()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


def supervise(children: list[Child]) -> None:
    started = {c.name: time.time() for c in children}
    for c in children:
        c.start()

    stopping = threading.Event()

    def shutdown(*_):
        stopping.set()

    signal.signal(signal.SIGINT, shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, shutdown)

    try:
        while not stopping.is_set():
            for child in children:
                if child.proc is None or child.proc.poll() is None:
                    continue
                code = child.proc.returncode
                uptime = time.time() - started[child.name]
                if uptime >= HEALTHY_AFTER:
                    child.backoff = MIN_BACKOFF
                child.restarts += 1
                LOG.warning("%s exited with code %s after %.0fs — restart #%d in %ds",
                            child.name, code, uptime, child.restarts, child.backoff)
                if stopping.wait(child.backoff):
                    return
                child.backoff = min(MAX_BACKOFF, child.backoff * 2)
                started[child.name] = time.time()
                child.start()
            stopping.wait(2)
    finally:
        for child in children:
            child.stop()
        LOG.info("all children stopped")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-bot", action="store_true", help="skip the Telegram bot")
    ap.add_argument("--no-webapp", action="store_true", help="skip the Mini App server")
    ap.add_argument("--no-engine", action="store_true", help="skip the paper engine")
    args = ap.parse_args()
    config.use_utf8_stdio()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [supervisor] %(message)s")

    py = sys.executable
    children: list[Child] = []
    if not args.no_engine:
        children.append(Child("engine", [py, "-u", str(HERE / "run_engine.py")]))
    if not args.no_webapp:
        children.append(Child("webapp", [py, "-u", str(HERE / "webapp.py")]))
    if not args.no_bot:
        if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID:
            children.append(Child("bot", [py, "-u", str(HERE / "telegram_bot.py")]))
        else:
            LOG.warning("bot disabled: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing from .env")
    if not children:
        raise SystemExit("nothing to run")

    LOG.info("config: %s", config.ENV_PATH)
    LOG.info("mini app: http://%s:%d%s", config.WEBAPP_HOST, config.WEBAPP_PORT,
             f"  (public {config.WEBAPP_PUBLIC_URL})" if config.WEBAPP_PUBLIC_URL else "")
    supervise(children)


if __name__ == "__main__":
    main()
