#!/usr/bin/env python3
"""Expose the local Mini App over HTTPS via a Cloudflare quick tunnel.

Telegram refuses to open a Mini App unless the URL is https, so the panel needs a
public origin. This wraps `cloudflared tunnel --url`, waits for the generated
`https://….trycloudflare.com` hostname, and hands it back to the caller.

The URL is ephemeral: a new one is issued every run, which is why the supervisor
pushes it to the bot at startup rather than expecting it to be pinned in .env.

    python gmgn/tunnel.py            # print the URL and keep the tunnel open
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402

LOG = logging.getLogger("tunnel")
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
# cloudflared prints the hostname before it has an edge connection, so waiting on the
# URL alone yields a URL that answers HTTP 530. Registration is the real ready signal.
REGISTERED_RE = re.compile(r"Registered tunnel connection|Connection [0-9a-f-]+ registered")
START_TIMEOUT = config.get_int("TUNNEL_START_TIMEOUT", 45)
# quic needs outbound UDP 7844, which plenty of networks drop; http2 rides TCP 443.
PROTOCOLS = [p.strip() for p in config.get("TUNNEL_PROTOCOLS", "quic,http2").split(",") if p.strip()]


def find_cloudflared() -> str | None:
    for name in ("cloudflared", "cloudflared.exe", "cloudflared.cmd"):
        found = shutil.which(name)
        if found:
            return found
    # npm's global bin is often absent from a non-login shell's PATH on Windows.
    if os.name == "nt":
        for suffix in ("cmd", "exe"):
            candidate = os.path.expandvars(rf"%APPDATA%\npm\cloudflared.{suffix}")
            if os.path.isfile(candidate):
                return candidate
    return None


class Tunnel:
    def __init__(self, port: int | None = None):
        self.port = port or config.WEBAPP_PORT
        self.proc: subprocess.Popen | None = None
        self.url: str = ""
        self._ready = threading.Event()

    def start(self) -> str:
        """Launch cloudflared and return a URL that actually serves, or "" on failure.

        Each transport in PROTOCOLS is tried in turn: a network that blocks outbound
        UDP 7844 fails quic but works over http2.
        """
        exe = find_cloudflared()
        if not exe:
            LOG.warning("cloudflared not found — install it to publish the Mini App "
                        "(npm install -g cloudflared)")
            return ""
        for protocol in PROTOCOLS:
            if self._attempt(exe, protocol):
                return self.url
            LOG.warning("tunnel over %s did not connect", protocol)
            self.stop()
        LOG.error("could not establish a tunnel over any of: %s", ", ".join(PROTOCOLS))
        return ""

    def _attempt(self, exe: str, protocol: str) -> bool:
        self.url = ""
        self._ready.clear()
        LOG.info("starting tunnel (protocol=%s)", protocol)
        self.proc = subprocess.Popen(
            [exe, "tunnel", "--no-autoupdate", "--protocol", protocol,
             "--url", f"http://127.0.0.1:{self.port}"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        threading.Thread(target=self._read, daemon=True, name=f"tunnel-{protocol}").start()
        return self._ready.wait(START_TIMEOUT) and bool(self.url)

    def _read(self) -> None:
        proc = self.proc
        if not proc or not proc.stdout:
            return
        for line in proc.stdout:
            if not self.url:
                m = URL_RE.search(line)
                if m:
                    self.url = m.group(0)
            if self.url and not self._ready.is_set() and REGISTERED_RE.search(line):
                LOG.info("tunnel up: %s", self.url)
                self._ready.set()
            # cloudflared is chatty; only surface problems once we are connected.
            if self._ready.is_set() and "ERR" in line:
                LOG.warning("cloudflared: %s", line.rstrip())

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=10)
            except Exception:
                self.proc.kill()


def main() -> None:
    config.use_utf8_stdio()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    t = Tunnel()
    url = t.start()
    if not url:
        raise SystemExit(1)
    print(f"\nMini App URL: {url}\nWEBAPP_PUBLIC_URL=\"{url}\"\n", flush=True)
    try:
        while t.proc and t.proc.poll() is None:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        t.stop()


if __name__ == "__main__":
    main()
