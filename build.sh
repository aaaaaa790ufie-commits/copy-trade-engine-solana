#!/usr/bin/env bash
# Sentinel — Build & Run Script
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== Sentinel Build ==="

# 1. Environment
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo "[!] Created .env from .env.example — paste your API keys before running."
fi

# 2. Python dependencies
echo "[*] Installing Python deps..."
python -m venv .venv 2>/dev/null || true
source .venv/Scripts/activate 2>/dev/null || source .venv/bin/activate 2>/dev/null || true
pip install -q -r discovery/requirements.txt 2>/dev/null
pip install -q -r dashboard/requirements.txt 2>/dev/null || true

# 3. Rust release build
echo "[*] Building Rust binary..."
export PATH="$PATH:$HOME/.cargo/bin"
if [ -d "/c/Users/Admin/mingw-tools-full/mingw64/bin" ]; then
    export PATH="/c/Users/Admin/mingw-tools-full/mingw64/bin:$PATH"
fi
OPENSSL_DIR="${OPENSSL_DIR:-}" cargo build --release 2>&1 | tail -3

# 4. Discovery run (optional, needs RPC keys)
echo "[*] Discovery module ready. Run:  python discovery/run_discovery.py"
echo "[*] Scorer module ready.  Run:  python scorer/run_scorer.py"
echo "[*] Dashboard ready.      Run:  streamlit run dashboard/app.py"
echo "[*] Engine ready.         Run:  ./target/release/sentinel"
echo ""
echo "=== Build complete ==="
