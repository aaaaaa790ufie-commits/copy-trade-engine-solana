#!/usr/bin/env python3
"""Build a fresh Solana quality-wallet file from GMGN OpenAPI data.

The collector widens the candidate universe with Smart Money, KOL,
trending/trench token lists, and each token's top traders. It then verifies
candidates with GMGN 30d stats plus a 7d activity gate before atomically
rewriting wallets-quality.txt. It never submits swaps and never needs a
GMGN private key.
"""
from __future__ import annotations
import argparse, json, logging, os, subprocess, tempfile, time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOG = logging.getLogger("gmgn-mass-discovery")
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "wallets-quality.txt"
DEFAULT_SEEDS = ROOT / "data" / "seed_wallets_sol.txt"

def cli(args: list[str]) -> Any:
    proc = subprocess.run(["gmgn-cli", *args, "--raw"], capture_output=True, text=True, timeout=60)
    if proc.returncode:
        raise RuntimeError((proc.stderr or proc.stdout).strip())
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    return json.loads(lines[-1]) if lines else {}

def unwrap(value: Any) -> Any:
    while isinstance(value, dict) and isinstance(value.get("data"), (dict, list)):
        value = value["data"]
    return value

def rows(value: Any) -> list[dict[str, Any]]:
    value = unwrap(value)
    if isinstance(value, list):
        return [x for x in value if isinstance(x, dict)]
    if isinstance(value, dict):
        for key in ("list", "items", "tokens", "wallets", "result"):
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
            if "winrate" in key.lower() and parsed > 1:
                parsed /= 100
            return parsed
        except (TypeError, ValueError):
            continue
    return default

def wallet_address(obj: dict[str, Any]) -> str:
    return str(obj.get("maker") or obj.get("wallet") or obj.get("wallet_address") or obj.get("address") or "")

def token_address(obj: dict[str, Any]) -> str:
    return str(obj.get("base_address") or obj.get("address") or obj.get("token_address") or "")

def last_seen(obj: dict[str, Any]) -> int:
    return int(number(obj, "last_active_timestamp", "last_seen", "timestamp", "open_timestamp"))

def discover_candidates(args: argparse.Namespace) -> dict[str, set[str]]:
    candidates: dict[str, set[str]] = defaultdict(set)
    def add(source: str, payload: Any) -> None:
        for item in rows(payload):
            wallet = wallet_address(item)
            if wallet:
                candidates[wallet].add(source)

    add("smartmoney", cli(["track", "smartmoney", "--chain", "sol", "--limit", str(args.feed_limit)]))
    add("kol", cli(["track", "kol", "--chain", "sol", "--limit", str(args.feed_limit)]))

    token_ids: set[str] = set()
    for interval in ("5m", "1h", "6h", "24h"):
        for order_by in ("smart-degen-count", "renowned-count", "volume"):
            payload = cli([
                "market", "trending", "--chain", "sol", "--interval", interval,
                "--limit", str(args.token_limit), "--order-by", order_by,
                "--direction", "desc", "--filter", "not_risk",
            ])
            for item in rows(payload):
                token = token_address(item)
                if token:
                    token_ids.add(token)

    for trench_type in ("new_creation", "near_completion", "completed"):
        payload = cli([
            "market", "trenches", "--chain", "sol", "--type", trench_type,
            "--limit", str(args.token_limit),
        ])
        for item in rows(payload):
            token = token_address(item)
            if token:
                token_ids.add(token)

    token_ids = set(list(token_ids)[: args.max_tokens])
    LOG.info("candidate feeds: %d wallets, %d tokens", len(candidates), len(token_ids))
    for index, token in enumerate(sorted(token_ids), 1):
        try:
            add(f"token_traders:{token}", cli([
                "token", "traders", "--chain", "sol", "--address", token,
                "--limit", str(args.trader_limit),
            ]))
        except Exception as exc:
            LOG.warning("token traders failed %s (%d/%d): %s", token, index, len(token_ids), exc)
        time.sleep(args.delay)
    LOG.info("raw unique candidates: %d", len(candidates))
    return candidates

def load_seeds(path: Path) -> dict[str, set[str]]:
    result: dict[str, set[str]] = defaultdict(set)
    if not path.exists():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if value and not value.startswith("#"):
            result[value].add("manual_seed")
    return result

def fetch_stats(wallets: list[str], args: argparse.Namespace) -> dict[str, dict[str, Any]]:
    stats: dict[str, dict[str, Any]] = {}
    for start in range(0, len(wallets), args.stats_batch):
        batch = wallets[start : start + args.stats_batch]
        try:
            payload = cli(["portfolio", "stats", "--chain", "sol", "--wallet", *batch, "--period", "30d"])
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
    active_7d = int(number(stat, "buy_count_7d", "txs_7d", "trades_7d", "active_tx_count_7d"))
    total_30d = int(number(stat, "buy_count_30d", "txs_30d", "trades_30d", "buy_count"))
    return wr >= args.min_winrate and active_7d >= args.min_7d_trades and total_30d >= args.min_30d_trades, wr, active_7d, total_30d

def write_quality(path: Path, qualified: list[tuple[str, float, int, str]], min_wr: float, dry_run: bool) -> None:
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines = [f"# Solana quality wallets | winrate>={min_wr:.2f} | {len(qualified)} wallets | {stamp}", "# address | source | winrate | last_seen_ts"]
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
    parser.add_argument("--min-7d-trades", type=int, default=1)
    parser.add_argument("--min-30d-trades", type=int, default=5)
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
