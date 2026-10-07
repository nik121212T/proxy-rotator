#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then
  echo 'Usage: ./scripts/start.sh [--help]'
  echo 'Requires Python 3.11+ and .env (copy .env.example and set MASTER_SECRET).'
  echo 'Creates .venv, installs changed requirements, then starts the local app.'
  exit 0
fi
if [[ $# -ne 0 ]]; then
  echo '[error] Unknown argument. Use --help.' >&2
  exit 1
fi
if [[ ! -f .env ]]; then
  echo '[error] .env missing. Copy .env.example to .env and set MASTER_SECRET.' >&2
  exit 78
fi
if [[ ! -x .venv/bin/python ]]; then
  command -v python3 >/dev/null 2>&1 || { echo '[error] Install Python 3.11+ first.' >&2; exit 1; }
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || {
    echo '[error] Python 3.11+ is required.' >&2; exit 1;
  }
  echo '[setup] creating venv...'
  python3 -m venv .venv
fi
.venv/bin/python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || {
  echo '[error] The virtual environment requires Python 3.11+.' >&2; exit 1;
}
if [[ ! -f .venv/.deps-installed || requirements.txt -nt .venv/.deps-installed ]]; then
  echo '[setup] installing deps...'
  .venv/bin/python -m pip install --quiet -r requirements.txt
  touch .venv/.deps-installed
fi
mkdir -p data
echo '[run] starting proxy-rotator...'
# The validated Settings loader reads BIND_HOST/ADMIN_PORT from .env, including
# defaults, quoted values and comments. Never source credential-bearing .env.
# run() starts Uvicorn and preserves EX_CONFIG=78 for configuration failures.
exec .venv/bin/python -m app.main
