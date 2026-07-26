#!/usr/bin/env python3
"""Build a fresh Solana quality-wallet file from GMGN OpenAPI data.

The collector widens the candidate universe with Smart Money, KOL,
trending/trench token lists, and each token's top traders, then verifies
candidates with GMGN 30d stats before atomically rewriting
wallets-quality.txt. It never submits swaps.

Three gates decide who qualifies: 30d win rate (--min-winrate, default 0.50),
30d sample size (--min-30d-trades, default 3), and 7d activity
(--min-7d-trades, default 0, i.e. off — GMGN populates those fields sparsely
and requiring them drops most otherwise-qualified wallets).

Credentials come from the repo-local .env via config.py, the same as the
engine — see gmgn_cli().
"""
from __future__ import annotations
import argparse, logging, os, sys, tempfile, time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
from paper_engine import _is_winrate_key, gmgn_cli, valid_address  # noqa: E402

LOG = logging.getLogger("gmgn-mass-discovery")
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "wallets-quality.txt"
DEFAULT_SEEDS = ROOT / "data" / "seed_wallets_sol.txt"
CLI_TIMEOUT = config.get_int("GMGN_DISCOVERY_CLI_TIMEOUT", 60)

def cli(args: list[str]) -> Any:
    """Shared with the engine, so this tool uses the same repo-local credentials.

    It previously spawned gmgn-cli with no env=, which meant it silently fell back to
    the machine-wide ~/.config/gmgn keys instead of the project's .env.
    """
    return gmgn_cli(args, timeout=CLI_TIMEOUT)

def _cli_retry(args: list[str], retries: int = 3, delay: float = 1.0) -> Any:
    last_err: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            return cli(args)
        except RuntimeError as e:
            last_err = e
            LOG.warning("cli attempt %d/%d failed: %s", attempt + 1, retries, e)
            time.sleep(delay * (attempt + 1))
    raise last_err if last_err else RuntimeError(f"gmgn-cli produced no result for {args[:2]}")

def unwrap(value: Any) -> Any:
    while isinstance(value, dict) and isinstance(value.get("data"), (dict, list)):
        value = value["data"]
    return value

def rows(value: Any) -> list[dict[str, Any]]:
    value = unwrap(value)
    if isinstance(value, list):
        return [x for x in value if isinstance(x, dict)]
    if isinstance(value, dict):
        for key in ("list", "items", "tokens", "wallets", "result", "rank"):
            if isinstance(value.get(key), list):
                return [x for x in value[key] if isinstance(x, dict)]
        return [value] if value else []
    return []

def number(obj: dict[str, Any], *keys: str, default: float = 0.0) -> float:
    for key in keys:
        value: Any = obj
        for part in key.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        if value is None:
            continue
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        # Underscores stripped before the check: "winrate" in "win_rate" is False, so a
        # percentage under that spelling was never scaled and every such wallet sailed
        # past --min-winrate. See paper_engine._is_winrate_key.
        if _is_winrate_key(key):
            if parsed > 1:
                parsed /= 100
            if not 0 <= parsed <= 1:
                LOG.warning("ignoring out-of-range winrate %r from %s", value, key)
                continue
        return parsed
    return default

def wallet_address(obj: dict[str, Any]) -> str:
    """First well-formed address among the field names GMGN uses, else "".

    The result is written to wallets-quality.txt, which is committed, so malformed feed
    data is rejected here rather than persisted.
    """
    for key in ("maker", "wallet", "wallet_address", "address"):
        value = obj.get(key)
        if value is not None and valid_address(str(value)):
            return str(value)
    return ""

def token_address(obj: dict[str, Any]) -> str:
    for key in ("base_address", "address", "token_address"):
        value = obj.get(key)
        if value is not None and valid_address(str(value)):
            return str(value)
    return ""

def last_seen(obj: dict[str, Any]) -> int:
    return int(number(obj, "last_active_timestamp", "last_seen", "timestamp", "open_timestamp"))

def discover_candidates(args: argparse.Namespace) -> dict[str, set[str]]:
    candidates: dict[str, set[str]] = defaultdict(set)
    def add(source: str, payload: Any) -> None:
        for item in rows(payload):
            wallet = wallet_address(item)
            if wallet:
                candidates[wallet].add(source)

    add("smartmoney", _cli_retry(["track", "smartmoney", "--chain", "sol", "--limit", str(args.feed_limit)]))
    time.sleep(args.delay)
    add("kol", _cli_retry(["track", "kol", "--chain", "sol", "--limit", str(args.feed_limit)]))
    time.sleep(args.delay)

    token_ids: set[str] = set()
    for interval in ("5m", "1h", "6h", "24h"):
        for order_by in ("smart_degen_count", "renowned_count", "volume"):
            try:
                payload = _cli_retry(["market", "trending", "--chain", "sol", "--interval", interval,
                               "--limit", str(args.token_limit), "--order-by", order_by,
                               "--direction", "desc"])
                for item in rows(payload):
                    token = token_address(item)
                    if token:
                        token_ids.add(token)
            except Exception as exc:
                LOG.warning("trending %s %s: %s", interval, order_by, exc)
            time.sleep(args.delay)

    for trench_type in ("new_creation", "near_completion", "completed"):
        try:
            payload = _cli_retry(["market", "trenches", "--chain", "sol", "--type", trench_type])
            for item in rows(payload.get(trench_type, [])):
                token = token_address(item)
                if token:
                    token_ids.add(token)
        except Exception as exc:
            LOG.warning("trenches %s: %s", trench_type, exc)
        time.sleep(args.delay)

    token_ids = set(list(token_ids)[: args.max_tokens])
    LOG.info("candidate feeds: %d wallets, %d tokens", len(candidates), len(token_ids))
    if args.trader_limit > 0:
        for index, token in enumerate(sorted(token_ids), 1):
            try:
                add(f"token_traders:{token[:8]}", _cli_retry(["token", "traders", "--chain", "sol",
                                                       "--address", token,
                                                       "--limit", str(args.trader_limit)]))
            except Exception as exc:
                LOG.warning("token traders failed %s (%d/%d): %s", token, index, len(token_ids), exc)
            time.sleep(args.delay)
    else:
        LOG.info("skipping token traders (trader_limit=0)")
    LOG.info("raw unique candidates: %d", len(candidates))
    return candidates

def load_seeds(path: Path) -> dict[str, set[str]]:
    result: dict[str, set[str]] = defaultdict(set)
    if not path.exists():
        return result
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        if not valid_address(value):
            LOG.warning("%s:%d is not a Solana address, skipping: %r", path.name, lineno, value[:64])
            continue
        result[value].add("manual_seed")
    return result

def fetch_stats(wallets: list[str], args: argparse.Namespace) -> dict[str, dict[str, Any]]:
    stats: dict[str, dict[str, Any]] = {}
    total = len(wallets)
    for start in range(0, total, args.stats_batch):
        batch = wallets[start : start + args.stats_batch]
        # This is the long phase — thousands of wallets at args.delay apiece. Without a
        # progress line a full run looks indistinguishable from a hang.
        if start and start % (args.stats_batch * 20) == 0:
            LOG.info("verified %d/%d wallets (%d qualified so far)", start, total, len(stats))
        try:
            payload = _cli_retry(["portfolio", "stats", "--chain", "sol", "--wallet", *batch, "--period", "30d"])
            got = rows(payload)
            for item in got:
                wallet = wallet_address(item)
                if wallet:
                    stats[wallet] = item
            if len(batch) == 1 and len(got) == 1 and batch[0] not in stats:
                stats[batch[0]] = got[0]
        except Exception as exc:
            LOG.warning("stats batch %d-%d failed: %s", start + 1, start + len(batch), exc)
        time.sleep(args.delay)
    return stats

def qualifies(stat: dict[str, Any], args: argparse.Namespace) -> tuple[bool, float, int, int]:
    wr = number(stat, "winrate", "win_rate", "pnl_stat.winrate", "pnl_stat.win_rate")
    active_7d = int(number(stat, "buy_count_7d", "txs_7d", "trades_7d", "active_tx_count_7d", "buy_7d"))
    total_30d = int(number(stat, "buy_count_30d", "txs_30d", "trades_30d", "buy_count", "buy_count_30", "buy"))
    return wr >= args.min_winrate and active_7d >= args.min_7d_trades and total_30d >= args.min_30d_trades, wr, active_7d, total_30d

def write_quality(path: Path, qualified: list[tuple[str, float, int, str]], min_wr: float, dry_run: bool) -> None:
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines = [f"# Solana quality wallets | winrate>={min_wr:.2f} | {len(qualified)} wallets | {stamp}",
             "# address | source | winrate | last_seen_ts"]
    lines.extend(f"{wallet} | {source} | {wr:.4f} | {seen}" for wallet, wr, seen, source in qualified)
    content = "\n".join(lines) + "\n"
    if dry_run:
        LOG.info("dry-run: would write %d wallets to %s", len(qualified), path)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix="wallets-quality.", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)

def main() -> int:
    parser = argparse.ArgumentParser(description="Discover and verify a large current Solana wallet universe via GMGN")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--seed-file", type=Path, default=DEFAULT_SEEDS)
    parser.add_argument("--target", type=int, default=3000)
    parser.add_argument("--max-tokens", type=int, default=300)
    parser.add_argument("--token-limit", type=int, default=100)
    parser.add_argument("--trader-limit", type=int, default=100)
    parser.add_argument("--feed-limit", type=int, default=200)
    parser.add_argument("--stats-batch", type=int, default=10)
    parser.add_argument("--min-winrate", type=float, default=0.50)
    parser.add_argument("--min-7d-trades", type=int, default=0)
    parser.add_argument("--min-30d-trades", type=int, default=3)
    parser.add_argument("--delay", type=float, default=0.35)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    candidates = discover_candidates(args)
    for wallet, sources in load_seeds(args.seed_file).items():
        candidates[wallet].update(sources)
    ordered = sorted(candidates, key=lambda w: (-len(candidates[w]), w))
    LOG.info("verifying %d unique wallets with 30d stats", len(ordered))
    stats = fetch_stats(ordered, args)

    qualified: list[tuple[str, float, int, str]] = []
    for wallet in ordered:
        stat = stats.get(wallet)
        if not stat:
            continue
        ok, wr, active_7d, _total_30d = qualifies(stat, args)
        if ok:
            qualified.append((wallet, wr, last_seen(stat), ",".join(sorted(candidates[wallet]))[:200]))
    qualified.sort(key=lambda item: (-item[1], -item[2], item[0]))
    write_quality(args.output, qualified[: args.target], args.min_winrate, args.dry_run)
    LOG.info("verified quality wallets: %d", min(len(qualified), args.target))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
