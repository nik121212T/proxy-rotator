@echo off
setlocal

cd /d "%~dp0..\"

if not exist .venv (
  echo [setup] creating venv...
  python -m venv .venv
)

call .venv\Scripts\activate.bat

if not exist .venv\.deps-installed (
  echo [setup] installing deps...
  pip install --quiet -r requirements.txt
  echo done > .venv\.deps-installed
)

if not exist .env (
  echo [error] .env missing. Copy .env.example to .env and set MASTER_SECRET.
  exit /b 78
)

if not exist data mkdir data

echo [run] starting proxy-rotator...
python -m uvicorn app.main:app --host 127.0.0.1 --port 8080
