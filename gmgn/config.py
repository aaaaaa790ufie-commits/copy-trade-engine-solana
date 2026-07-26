#!/usr/bin/env python3
"""Local-first configuration for Sentinel.

Every credential and every tunable this project uses is resolved here, from a
single `.env` file living in the repo root. Nothing is read from a machine-wide
location at runtime and nothing needs to be `export`ed into the shell first —
importing this module is enough.

Precedence: real process environment > repo `.env` > built-in default. That way
a one-off `GMGN_ENTRY_SCORE=0.4 python ...` still overrides the file.

The GMGN API credentials are handled the same way: `gmgn_env()` builds the
environment handed to the `gmgn-cli` subprocess, injecting the keys from the
local `.env`. Use `python gmgn/config.py --import-gmgn` once to copy an existing
machine-wide `~/.config/gmgn/.env` into the repo-local file.
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = Path(os.getenv("SENTINEL_ENV_FILE", str(ROOT / ".env")))

# Keys that must never be echoed back in logs, /config output or the web API.
SECRET_KEYS = ("TOKEN", "KEY", "SECRET", "PASSWORD", "SEED", "MNEMONIC")


def _parse_env_file(path: Path) -> dict[str, str]:
    """Minimal dotenv parser: KEY=VALUE, `export ` prefix, quotes, multi-line values.

    A trailing ` #` comment is stripped from an unquoted value but kept inside quotes,
    per the usual dotenv convention — `POLL=15  # fast` is 15, while a token containing
    a hash survives as long as it is quoted.

    Quoted values may span lines. `gmgn-cli config --apply` writes GMGN_PRIVATE_KEY as a
    PEM block, and reading only the first physical line silently truncated it to
    `"-----BEGIN PRIVATE KEY-----` — a corrupt credential that still looked plausible.
    """
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        i += 1
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        quote = value[0] if value[:1] in ("'", '"') else ""
        if quote:
            if len(value) >= 2 and value[-1] == quote:
                value = value[1:-1]
            else:
                # Opening quote with no closing one: keep taking raw lines until it closes.
                parts = [value[1:]]
                while i < len(lines):
                    nxt = lines[i]
                    i += 1
                    if nxt.rstrip().endswith(quote):
                        parts.append(nxt.rstrip()[:-1])
                        break
                    parts.append(nxt)
                else:
                    print(f"warning: {path}: unterminated quote for {key}", file=sys.stderr)
                value = "\n".join(parts)
        else:
            value = value.split(" #", 1)[0].split("\t#", 1)[0].strip()
        out[key] = value
    return out


FILE_ENV = _parse_env_file(ENV_PATH)


def _warn_if_world_readable(path: Path) -> None:
    """POSIX only — on Windows ACLs already keep the profile directory private."""
    if os.name == "nt" or not path.is_file():
        return
    if path.stat().st_mode & (stat.S_IRGRP | stat.S_IROTH):
        print(f"warning: {path} is readable by other users; run chmod 600 {path}", file=sys.stderr)


_warn_if_world_readable(ENV_PATH)


def use_utf8_stdio() -> None:
    """Make stdout/stderr UTF-8 tolerant.

    Log lines and Telegram messages are Russian and contain emoji. On a Windows
    console defaulting to cp1251 the first such line raises UnicodeEncodeError and
    kills the process, so every entrypoint calls this first.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")


def get(key: str, default: str = "") -> str:
    """Process env wins, then repo `.env`, then the default."""
    value = os.environ.get(key)
    if value is None or value == "":
        value = FILE_ENV.get(key, default)
    return value


def get_int(key: str, default: int) -> int:
    try:
        return int(float(get(key, str(default))))
    except (TypeError, ValueError):
        return default


def get_float(key: str, default: float) -> float:
    try:
        return float(get(key, str(default)))
    except (TypeError, ValueError):
        return default


def get_bool(key: str, default: bool = False) -> bool:
    raw = get(key, "1" if default else "0").strip().lower()
    return raw in ("1", "true", "yes", "on")


def get_list(key: str, default: str) -> list[str]:
    return [x.strip() for x in get(key, default).split(",") if x.strip()]


def is_secret(key: str) -> bool:
    return any(marker in key.upper() for marker in SECRET_KEYS)


def mask(value: str) -> str:
    """Render a credential safe to print: first 4 chars, then a fixed-width blank."""
    if not value:
        return "(unset)"
    return f"{value[:4]}...({len(value)} chars)"


# --------------------------------------------------------------------------
# GMGN API credentials
# --------------------------------------------------------------------------

# gmgn-cli reads these from its own environment; we supply them from the repo.
GMGN_CRED_KEYS = ("GMGN_API_KEY", "GMGN_PRIVATE_KEY", "GMGN_API_SECRET", "GMGN_WALLET_ADDRESS")
# GMGN_PRIVATE_KEY is what signs a swap. Every call this project makes is a read —
# feed, stats, token info, KOL — and all of them were verified to work without it.
# Withholding it means a paper engine cannot submit a transaction even if some future
# bug asked it to: the capability is absent from the subprocess, not merely unused.
GMGN_SIGNING_KEYS = ("GMGN_PRIVATE_KEY",)
GMGN_MACHINE_ENV = Path.home() / ".config" / "gmgn" / ".env"


def gmgn_env(allow_signing: bool = False) -> dict[str, str]:
    """Environment for the `gmgn-cli` subprocess, with repo-local credentials injected.

    The signing key is omitted unless explicitly requested. Nothing in this project
    requests it — the parameter exists so that a caller which genuinely needs to sign
    has to say so at the call site, where it is reviewable.
    """
    env = dict(os.environ)
    for key in GMGN_CRED_KEYS:
        if key in GMGN_SIGNING_KEYS and not allow_signing:
            env.pop(key, None)  # drop it even if the ambient environment carries one
            continue
        value = get(key)
        if value:
            env[key] = value
    return env


def _drop_keys(lines: list[str], keys: set[str]) -> list[str]:
    """Remove the given assignments, including the continuation lines of quoted blocks.

    Filtering line by line on the key name would leave the body of a multi-line PEM
    behind as orphaned text, which then parses as garbage on the next read.
    """
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip().removeprefix("export ").lstrip()
        key, sep, value = stripped.partition("=")
        i += 1
        if not sep or key.strip() not in keys:
            out.append(line)
            continue
        value = value.strip()
        quote = value[0] if value[:1] in ("'", '"') else ""
        if quote and not (len(value) >= 2 and value[-1] == quote):
            while i < len(lines):  # skip the rest of the quoted block
                if lines[i].rstrip().endswith(quote):
                    i += 1
                    break
                i += 1
    return out


def import_gmgn_credentials() -> int:
    """Copy a machine-wide gmgn-cli credential file into the repo-local `.env`.

    Values are moved verbatim without being printed. Returns the number of keys
    written; 0 means there was nothing new to import.
    """
    source = _parse_env_file(GMGN_MACHINE_ENV)
    if not source:
        print(f"nothing to import: {GMGN_MACHINE_ENV} not found or empty", file=sys.stderr)
        return 0
    existing = _parse_env_file(ENV_PATH)
    added = {k: v for k, v in source.items() if v and existing.get(k) != v}
    if not added:
        print("local .env already holds these GMGN credentials")
        return 0
    lines = ENV_PATH.read_text(encoding="utf-8-sig", errors="replace").splitlines() if ENV_PATH.is_file() else []
    kept = _drop_keys(lines, set(added))
    if kept and kept[-1].strip():
        kept.append("")
    kept.append("# GMGN API credentials (imported from machine-wide gmgn-cli config)")
    kept.extend(f'{k}="{v}"' for k, v in added.items())
    ENV_PATH.write_text("\n".join(kept) + "\n", encoding="utf-8")
    if os.name != "nt":
        ENV_PATH.chmod(0o600)
    print(f"imported {len(added)} credential(s) into {ENV_PATH}: {', '.join(sorted(added))}")
    return len(added)


# --------------------------------------------------------------------------
# Resolved settings — the single source of truth for engine, bot and web app
# --------------------------------------------------------------------------

DB_PATH = get("SENTINEL_DB", str(ROOT / "sentinel.db"))

# Telegram
TELEGRAM_BOT_TOKEN = get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = get("TELEGRAM_CHAT_ID")

# Mini app / local web server
WEBAPP_HOST = get("WEBAPP_HOST", "127.0.0.1")
WEBAPP_PORT = get_int("WEBAPP_PORT", 8770)
# Public HTTPS origin of the mini app (e.g. a cloudflared / ngrok tunnel).
# Telegram only opens WebApps over HTTPS, so the button is offered only when set.
WEBAPP_PUBLIC_URL = get("WEBAPP_PUBLIC_URL").rstrip("/")

# Engine
CHAINS = get_list("GMGN_CHAINS", "sol")
POLL_SECONDS = get_int("GMGN_POLL_SECONDS", 15)
ENTRY_SCORE = get_float("GMGN_ENTRY_SCORE", 1.0)
BUDGET_SOL = get_float("PAPER_BUDGET_SOL", 0.1)
STAKE_SOL = get_float("PAPER_TRADE_SIZE_SOL", 0.025)
CLUSTER_WINDOW = get_int("GMGN_CLUSTER_WINDOW_SECONDS", 1800)
COOLDOWN_SECONDS = get_int("GMGN_COOLDOWN_SECONDS", 420)
TRAILING_ACTIVATE_PCT = get_float("TRAILING_ACTIVATE_PCT", 25.0)
TRAILING_DISTANCE_PCT = get_float("TRAILING_DISTANCE_PCT", 15.0)
HARD_STOP_PCT = get_float("HARD_STOP_PCT", 45.0)
FEED_LIMIT = get_int("GMGN_FEED_LIMIT", 200)
MAX_HOLD_SECONDS = get_int("GMGN_MAX_HOLD_SECONDS", 3600)
ZERO_WINRATE_TTL = get_int("GMGN_ZERO_WINRATE_TTL_SECONDS", 3600)
PRICE_TTL = get_int("GMGN_PRICE_TTL_SECONDS", 60)
STATS_TTL = get_int("GMGN_STATS_TTL_SECONDS", 3600)


def summary() -> str:
    """Human-readable config dump. Anything is_secret() says is a credential is masked.

    Masking is driven by the key name rather than a hand-written list, so adding a
    credential to this dump cannot accidentally print it in full.
    """
    settings = [
        ("env file", str(ENV_PATH) + ("" if ENV_PATH.is_file() else "  (MISSING)")),
        ("database", DB_PATH),
        ("chains", ",".join(CHAINS)),
        ("poll seconds", POLL_SECONDS),
        ("entry score", ENTRY_SCORE),
        ("stake SOL", STAKE_SOL),
        ("hard stop %", HARD_STOP_PCT),
        ("trailing %", f"activate {TRAILING_ACTIVATE_PCT} / distance {TRAILING_DISTANCE_PCT}"),
        ("web app", f"http://{WEBAPP_HOST}:{WEBAPP_PORT}"),
        ("web app public", WEBAPP_PUBLIC_URL or "(unset - Telegram button disabled)"),
    ]
    credentials = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", *GMGN_CRED_KEYS]
    for key in credentials:
        value = get(key)
        settings.append((key, mask(value) if is_secret(key) else (value or "(unset)")))
    return "\n".join(f"{k:22} {v}" for k, v in settings)


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Inspect or populate the repo-local .env")
    ap.add_argument("--import-gmgn", action="store_true", help="copy ~/.config/gmgn/.env into the repo .env")
    args = ap.parse_args()
    if args.import_gmgn:
        import_gmgn_credentials()
        return
    print(summary())


if __name__ == "__main__":
    main()
