#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

# Create venv if missing
if [[ ! -d .venv ]]; then
  echo "[setup] creating venv..."
  python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

# Install / sync deps
if [[ ! -f .venv/.deps-installed ]] || [[ requirements.txt -nt .venv/.deps-installed ]]; then
  echo "[setup] installing deps..."
  pip install --quiet -r requirements.txt
  touch .venv/.deps-installed
fi

# Check env
if [[ ! -f .env ]]; then
  echo "[error] .env missing. Copy .env.example to .env and set MASTER_SECRET."
  exit 78
fi

mkdir -p data

echo "[run] starting proxy-rotator..."
exec python -m uvicorn app.main:app --host "$(grep -E '^BIND_HOST=' .env | cut -d= -f2 || echo 127.0.0.1)" --port "$(grep -E '^ADMIN_PORT=' .env | cut -d= -f2 || echo 8080)"
