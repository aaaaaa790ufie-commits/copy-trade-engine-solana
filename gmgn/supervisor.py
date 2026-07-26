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
    ap.add_argument("--tunnel", action="store_true",
                    help="publish the Mini App over HTTPS with cloudflared and hand the URL to the bot")
    args = ap.parse_args()
    config.use_utf8_stdio()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [supervisor] %(message)s")

    py = sys.executable
    tunnel = None
    children: list[Child] = []

    def publish(url: str) -> None:
        """Point every component at a newly issued public URL.

        Children read it from the environment, so the bot installs the Mini App
        button and the webapp turns on initData verification without anyone editing
        .env by hand. A free pinggy session expires hourly with a different
        hostname, so the bot is restarted to pick the new one up.
        """
        os.environ["WEBAPP_PUBLIC_URL"] = url
        os.environ["WEBAPP_REQUIRE_AUTH"] = "1"
        config.WEBAPP_PUBLIC_URL = url
        LOG.info("Mini App published at %s", url)
        for child in children:
            if child.name in ("bot", "webapp") and child.proc and child.proc.poll() is None:
                LOG.info("restarting %s for the new public URL", child.name)
                child.stop()  # the supervise loop notices the exit and restarts it

    if args.tunnel and not args.no_webapp:
        from tunnel import Tunnel

        tunnel = Tunnel(config.WEBAPP_PORT)
        url = tunnel.start()
        if url:
            os.environ["WEBAPP_PUBLIC_URL"] = url
            os.environ["WEBAPP_REQUIRE_AUTH"] = "1"
            config.WEBAPP_PUBLIC_URL = url
            LOG.info("Mini App published at %s (%s)", url, tunnel.active_provider)
        else:
            LOG.warning("tunnel unavailable — the Mini App stays local-only")

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
    if tunnel and tunnel.url:
        tunnel.watch(publish)
    try:
        supervise(children)
    finally:
        if tunnel:
            tunnel.close()


if __name__ == "__main__":
    main()
