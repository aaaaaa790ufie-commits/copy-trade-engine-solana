#!/usr/bin/env python3
"""Expose the local Mini App over HTTPS.

Telegram refuses to open a Mini App unless the URL is https, so the panel needs a
public origin. Two providers are supported and `auto` tries them in order:

  cloudflared  `cloudflared tunnel --url` -> https://….trycloudflare.com
               Needs outbound 7844. Some networks pass the TCP connect but reset
               the TLS handshake to the Cloudflare edge, which shows up as
               "TLS handshake with edge error: EOF" — that network cannot use it.

  pinggy       `ssh -p 443 -R0:…` -> https://….pinggy.link
               Rides SSH on 443, so it survives networks that filter the above.
               Free sessions expire after 60 minutes with a fresh URL each time,
               which is why the supervisor re-publishes on every reconnect.

    python gmgn/tunnel.py                  # print the URL and hold it open
    TUNNEL_PROVIDER=pinggy python gmgn/tunnel.py
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402

LOG = logging.getLogger("tunnel")

CF_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
# cloudflared prints the hostname several seconds before it has an edge connection;
# waiting on the hostname alone yields a URL that answers HTTP 530.
CF_READY_RE = re.compile(r"Registered tunnel connection|Connection [0-9a-f-]+ registered")
PINGGY_URL_RE = re.compile(r"https://[a-z0-9.-]+\.(?:pinggy\.link|pinggy-free\.link|free\.pinggy\.net)")

START_TIMEOUT = config.get_int("TUNNEL_START_TIMEOUT", 45)
PROVIDER = config.get("TUNNEL_PROVIDER", "auto").strip().lower()
# quic needs outbound UDP 7844; http2 rides TCP 7844. Both are commonly filtered.
CF_PROTOCOLS = [p.strip() for p in config.get("TUNNEL_PROTOCOLS", "quic,http2").split(",") if p.strip()]
PINGGY_HOST = config.get("PINGGY_HOST", "a.pinggy.io")


def _which(*names: str, npm_fallback: str = "") -> str | None:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    # npm's global bin is often absent from a non-login shell's PATH on Windows.
    if npm_fallback and os.name == "nt":
        for suffix in ("cmd", "exe"):
            candidate = os.path.expandvars(rf"%APPDATA%\npm\{npm_fallback}.{suffix}")
            if os.path.isfile(candidate):
                return candidate
    return None


def find_cloudflared() -> str | None:
    return _which("cloudflared", "cloudflared.exe", "cloudflared.cmd", npm_fallback="cloudflared")


def find_ssh() -> str | None:
    return _which("ssh", "ssh.exe")


class Tunnel:
    """A single public HTTPS origin for the local Mini App port."""

    def __init__(self, port: int | None = None, provider: str = ""):
        self.port = port or config.WEBAPP_PORT
        self.provider = (provider or PROVIDER).lower()
        self.proc: subprocess.Popen | None = None
        self.url: str = ""
        self.active_provider: str = ""
        self._ready = threading.Event()
        self._stopping = threading.Event()

    # -- public API ---------------------------------------------------------

    def start(self) -> str:
        """Bring up a tunnel and return a URL that actually serves, or "" on failure."""
        order = ["cloudflared", "pinggy"] if self.provider == "auto" else [self.provider]
        for name in order:
            attempt = getattr(self, f"_start_{name}", None)
            if attempt is None:
                LOG.error("unknown tunnel provider: %s", name)
                continue
            if attempt():
                self.active_provider = name
                return self.url
            LOG.warning("%s did not establish a tunnel", name)
            self.stop()
        LOG.error("no tunnel provider succeeded (tried: %s)", ", ".join(order))
        return ""

    def watch(self, on_url: "callable[[str], None]") -> threading.Thread:
        """Restart the tunnel whenever it drops, reporting each new URL.

        Free pinggy sessions expire on the hour with a different hostname, so the
        URL is not stable for the lifetime of the process.
        """

        def loop():
            while not self._stopping.is_set():
                if self.proc and self.proc.poll() is None:
                    self._stopping.wait(5)
                    continue
                LOG.warning("tunnel dropped — reconnecting")
                previous = self.url
                if self.start() and self.url != previous:
                    on_url(self.url)
                elif not self.url:
                    self._stopping.wait(30)

        t = threading.Thread(target=loop, daemon=True, name="tunnel-watch")
        t.start()
        return t

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=10)
            except Exception:
                self.proc.kill()

    def close(self) -> None:
        self._stopping.set()
        self.stop()

    # -- providers ----------------------------------------------------------

    def _start_cloudflared(self) -> bool:
        exe = find_cloudflared()
        if not exe:
            LOG.info("cloudflared not installed (npm install -g cloudflared)")
            return False
        for protocol in CF_PROTOCOLS:
            LOG.info("trying cloudflared (protocol=%s)", protocol)
            if self._spawn(
                [exe, "tunnel", "--no-autoupdate", "--protocol", protocol,
                 "--url", f"http://127.0.0.1:{self.port}"],
                CF_URL_RE, CF_READY_RE,
            ):
                return True
            self.stop()
        return False

    def _start_pinggy(self) -> bool:
        exe = find_ssh()
        if not exe:
            LOG.info("ssh not available — cannot use pinggy")
            return False
        LOG.info("trying pinggy over ssh:443")
        # Not os.devnull: OpenSSH on Windows treats "nul" as a relative path and
        # creates a literal file called `nul` in the working directory, which then
        # cannot be removed or indexed by git without a \\?\ prefix.
        known_hosts = Path(tempfile.gettempdir()) / "sentinel_known_hosts"
        return self._spawn(
            [exe, "-o", "StrictHostKeyChecking=no", "-o", f"UserKnownHostsFile={known_hosts}",
             "-o", "ServerAliveInterval=30", "-o", "ExitOnForwardFailure=yes",
             "-o", "ConnectTimeout=20", "-T", "-p", "443",
             f"-R0:127.0.0.1:{self.port}", PINGGY_HOST],
            PINGGY_URL_RE, PINGGY_URL_RE,
        )

    # -- process plumbing ---------------------------------------------------

    def _spawn(self, argv: list[str], url_re: re.Pattern, ready_re: re.Pattern) -> bool:
        self.url = ""
        self._ready.clear()
        self.proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        threading.Thread(target=self._read, args=(url_re, ready_re), daemon=True, name="tunnel-log").start()
        return self._ready.wait(START_TIMEOUT) and bool(self.url)

    def _read(self, url_re: re.Pattern, ready_re: re.Pattern) -> None:
        proc = self.proc
        if not proc or not proc.stdout:
            return
        for line in proc.stdout:
            if not self.url:
                m = url_re.search(line)
                if m:
                    self.url = m.group(0)
            if self.url and not self._ready.is_set() and ready_re.search(line):
                LOG.info("tunnel up: %s", self.url)
                self._ready.set()
            # These tools are chatty; only surface problems once we are connected.
            if self._ready.is_set() and "ERR" in line:
                LOG.warning("tunnel: %s", line.rstrip())


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
        t.close()


if __name__ == "__main__":
    main()
