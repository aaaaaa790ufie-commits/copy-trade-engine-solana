# GOAL: Sentinel — WARP/AmneziaWG tunnel for Helius, as an isolated service with health checks

Continuation of `~/sentinel`. Original `/goal` doc's rules still apply in
full (Fasol isolation, unfunded wallet, no live execution, no secrets in
git/prompts — this now explicitly includes WireGuard keys, same handling
as the trading wallet's private key).

## 0. Read-then-report

Read the existing `~/Desktop/warp-youtube-only.conf` to understand its
current split-tunnel rules before building the new one. Also check
https://github.com/ImMALWARE/bash-warp-generator's actual script content
before running it — same dependency-trust rule as always: read a new
external script before executing it, don't run-and-hope.

## 1. Generate a Helius-scoped WARP config, separate from the YouTube one

- Use bash-warp-generator to register a fresh WARP account and produce a
  new WireGuard config specifically for this project — don't reuse or
  overwrite `warp-youtube-only.conf`, keep them independent so a change to
  one doesn't risk breaking the other.
- If AmneziaWG's config format requires the junk-packet/DPI-evasion
  parameters on top of the base WireGuard config (as the existing YouTube
  config presumably has), carry those over — the point of AmneziaWG here
  is specifically to get past RKN's blocking of WARP's connection
  handshake, not just to run plain WireGuard.
- Store the resulting config file outside the git repo (e.g.
  `~/sentinel-secrets/` or similar, NOT `~/sentinel/`), and add its path
  to `.gitignore` patterns as a reminder even though it's outside the repo
  — treat the private key inside it with the same rule as the trading
  wallet's key: never in git, never pasted into a prompt.

## 2. Run the tunnel as an OS-level service, not inside Sentinel's process tree

- Set this up as a separate background process/service (Windows: a
  scheduled task or a simple always-on background script; not a thread or
  subprocess spawned and managed by the Rust binary or Python scripts).
  Sentinel's code should have zero knowledge of WARP/AmneziaWG — it just
  makes HTTP/WS calls to Helius as normal, and either the tunnel is
  routing them correctly at the OS network layer or it isn't.
- Document the exact start command for this service in `README.md` (how
  the human or the agent restarts it after a reboot), separate from the
  pipeline's own start instructions.

## 3. Health check before trusting Helius HTTP

- Before starting (or resuming) the Sentinel pipeline, run a simple
  connectivity check against Helius (the same `getHealth` curl-style call
  used earlier in this project) and log pass/fail explicitly.
- Add a periodic health check while the pipeline runs (e.g. every few
  minutes) that re-verifies Helius HTTP is reachable, and logs a clear
  warning — not a silent fallback — if it stops being reachable. The
  existing public-RPC fallback can still kick in, but the log must make
  it obvious *why* (tunnel down vs. some other cause), so a stale-tunnel
  problem doesn't get mistaken for a decoder or rate-limit problem again.

## 4. Build, restart, test

- Build the project as usual.
- Start the WARP/AmneziaWG service first, confirm the Section 3 health
  check passes, then start the Sentinel pipeline.
- Run for 15-30 minutes and report: health-check pass/fail history during
  the window, whether Helius HTTP calls succeeded (decode rate should
  approach the earlier working figure, not the degraded ~1-26% seen on
  public RPC), and current Helius credit usage rate from the dashboard
  (compare against the per-wallet-mentions fix's near-zero baseline from
  last session — some non-zero usage is expected and fine now that
  HTTP/getTransaction can actually reach Helius again).

## 5. What "done" means

Report back with: confirmation the new WARP config is stored outside git
with the key never having appeared in any commit or prompt, confirmation
the tunnel runs as an independent service, the health-check log excerpt
from Section 4's test window, and the resulting decode rate / credit
usage numbers. No live execution, no wallet funding, no touching Fasol.
