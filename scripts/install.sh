#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
cp -n .env.example .env

mkdir -p data

echo
echo "Install done."
echo "1. Edit .env — set MASTER_SECRET."
echo "2. Run ./scripts/start.sh"
