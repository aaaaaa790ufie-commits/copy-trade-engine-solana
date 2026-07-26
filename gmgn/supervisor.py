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
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402

HERE = Path(__file__).resolve().parent
LOG = logging.getLogger("supervisor")

MIN_BACKOFF = 3
MAX_BACKOFF = 120
# A child that survives this long is considered healthy, so its backoff resets.
HEALTHY_AFTER = 60
# How often the loop looks at every child. Backoff is a scheduled time, not a sleep,
# so this is the worst-case delay in noticing any single exit.
TICK = 1


class Child:
    def __init__(self, name: str, argv: list[str]):
        self.name = name
        self.argv = argv
        self.proc: subprocess.Popen | None = None
        self.backoff = MIN_BACKOFF
        self.restarts = 0
        # Set when we stop the child deliberately (e.g. a new public URL), so its exit
        # is not counted as a crash and does not inflate the restart backoff.
        self.expected_exit = False
        # When a crashed child is due back, or 0 if it is running. A time rather than a
        # sleep, so waiting out one child's backoff does not stop the others being
        # watched — see supervise_once.
        self.restart_at = 0.0

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

    def stop(self, expected: bool = False) -> None:
        """Terminate the child. `expected` marks a restart we asked for, so the
        supervise loop reports it as such instead of as a crash."""
        if not self.proc or self.proc.poll() is not None:
            # Already gone — we did not stop it, so it must stay a crash. Marking it
            # expected here would let the next genuine crash restart with no backoff.
            return
        self.expected_exit = expected
        LOG.info("stopping %s", self.name)
        try:
            self.proc.terminate()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()
            try:
                self.proc.wait(timeout=5)  # reap it; without this it lingers as a zombie
            except Exception:
                LOG.warning("%s did not exit after kill", self.name)


LOCAL_READY_TIMEOUT = 60


def wait_for_local(port: int, timeout: int = LOCAL_READY_TIMEOUT) -> bool:
    """Block until the Mini App answers on loopback, or the timeout expires."""
    url = f"http://127.0.0.1:{port}/api/health"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def bring_up_tunnel(tunnel, port: int, publish, wait=wait_for_local) -> None:
    """Publish the Mini App over HTTPS, once there is something to publish.

    The tunnel probes its own public URL for a real 200 before handing it out, so the
    server has to be listening first. This used to run before the webapp child was even
    created, let alone started, so on a clean boot the probe hit an empty port, spent the
    full START_TIMEOUT + PROBE_TIMEOUT failing, and logged "the Mini App stays
    local-only" — which was then untrue, because watch() reconnected a minute later and
    succeeded.

    watch() is installed whether or not the first attempt worked: a failed start must
    still be retried, and it is also the only thing that renews a free pinggy session
    when it expires on the hour.
    """
    if not wait(port):
        LOG.warning("Mini App did not answer on 127.0.0.1:%d — trying to publish anyway", port)
    if tunnel.start():
        publish(tunnel.url)
    else:
        LOG.warning("tunnel unavailable for now — the Mini App stays local-only")
    tunnel.watch(publish)


def supervise_once(children: list[Child], started: dict[str, float], now: float) -> list[str]:
    """One supervision pass. Returns the names started during it.

    Nothing here blocks. Backoff used to be a `wait()` inside this loop, which meant a
    child waiting out its delay also suspended the check on every other child: with the
    bot flapping at the 120s ceiling, an engine that died right after was left dead for
    up to two minutes. That is the one outage this whole file exists to prevent — every
    second the engine is not polling is a second open positions are not checked against
    their stops. So a crash schedules `restart_at` and the pass moves on.
    """
    restarted = []
    for child in children:
        if child.proc is None or child.proc.poll() is None:
            child.restart_at = 0.0   # running (or never started): nothing pending
            continue
        if child.expected_exit:
            # We asked for this one; restart immediately and leave backoff alone.
            child.expected_exit = False
            child.restart_at = 0.0
            LOG.info("restarting %s as requested", child.name)
            started[child.name] = now
            child.start()
            restarted.append(child.name)
            continue
        if not child.restart_at:
            # First pass that notices this exit: count it and schedule the retry.
            uptime = now - started[child.name]
            if uptime >= HEALTHY_AFTER:
                child.backoff = MIN_BACKOFF
            child.restarts += 1
            LOG.warning("%s exited with code %s after %.0fs — restart #%d in %ds",
                        child.name, child.proc.returncode, uptime, child.restarts, child.backoff)
            child.restart_at = now + child.backoff
            child.backoff = min(MAX_BACKOFF, child.backoff * 2)
            continue
        if now >= child.restart_at:
            child.restart_at = 0.0
            started[child.name] = now
            child.start()
            restarted.append(child.name)
    return restarted


def supervise(children: list[Child]) -> None:
    started = {c.name: time.time() for c in children}
    for c in children:
        c.start()

    stopping = threading.Event()

    def shutdown(*_):
        stopping.set()

    # Signal handlers can only be installed from the main thread; installing them is a
    # convenience for the CLI, not a requirement of the loop, so a non-main-thread caller
    # gets the same supervision without Ctrl+C handling rather than a ValueError.
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGINT, shutdown)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, shutdown)
    else:
        LOG.debug("not on the main thread — signal handlers not installed")

    try:
        while not stopping.is_set():
            supervise_once(children, started, time.time())
            stopping.wait(TICK)
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
                child.stop(expected=True)  # the supervise loop notices the exit and restarts it

    if args.tunnel and not args.no_webapp:
        # Decided before the webapp is spawned, not when the URL arrives. The webapp
        # computes REQUIRE_AUTH once at import, so a panel started auth-off and restarted
        # by publish() was reachable through the live tunnel, unauthenticated, for the
        # couple of seconds in between — 21:34:22 to 21:34:24 in the last boot log.
        # Asking for --tunnel is the decision to expose it; there is no window in which
        # that has been asked for and auth is not wanted.
        os.environ["WEBAPP_REQUIRE_AUTH"] = "1"

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

    if args.tunnel and not args.no_webapp:
        from tunnel import Tunnel

        # Off the main thread, so publishing waits for the webapp without holding up the
        # engine — which supervise() is about to start, and whose downtime is what this
        # file exists to minimise. publish() sets the environment and restarts the bot and
        # webapp, so both come back knowing the public URL and with auth switched on.
        tunnel = Tunnel(config.WEBAPP_PORT)
        threading.Thread(target=bring_up_tunnel, name="tunnel-up", daemon=True,
                         args=(tunnel, config.WEBAPP_PORT, publish)).start()
    try:
        supervise(children)
    finally:
        if tunnel:
            tunnel.close()


if __name__ == "__main__":
    main()
