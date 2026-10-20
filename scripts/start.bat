@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0.." || exit /b 1
if "%~1"=="--help" goto help
if "%~1"=="-h" goto help
if not "%~1"=="" (
  echo [error] Unknown argument. Use --help.
  exit /b 1
)
if not exist .env (
  echo [error] .env missing. Copy .env.example to .env and set MASTER_SECRET.
  exit /b 78
)
if exist ".updates\active.json" goto updated_environment
if exist ".venv\Scripts\python.exe" goto check_python
where python >nul 2>nul
if errorlevel 1 goto python_error
python -c "import sys, platform, struct; sys.exit(0 if (3, 11) <= sys.version_info < (3, 14) and struct.calcsize('P') == 8 and platform.machine().lower() in ('amd64', 'x86_64') else 1)"
if errorlevel 1 goto python_error
echo [setup] creating venv...
python -m venv .venv
if errorlevel 1 exit /b 1
:check_python
".venv\Scripts\python.exe" -c "import sys, platform, struct; sys.exit(0 if (3, 11) <= sys.version_info < (3, 14) and struct.calcsize('P') == 8 and platform.machine().lower() in ('amd64', 'x86_64') else 1)"
if errorlevel 1 goto python_error
".venv\Scripts\python.exe" -c "from pathlib import Path; import sys; p=Path('.venv/.deps-installed'); sys.exit(10 if p.exists() and p.stat().st_mtime >= Path('requirements.txt').stat().st_mtime else 0)"
if errorlevel 11 exit /b 1
if errorlevel 10 goto launch
if errorlevel 1 exit /b 1
echo [setup] installing deps...
".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -c "from pathlib import Path; Path('.venv/.deps-installed').touch()"
if errorlevel 1 exit /b 1
:launch
if not exist data mkdir data
if not exist data exit /b 1
echo [run] starting proxy-rotator...
rem Never interpolate credential-bearing .env values into shell commands.
findstr /R /C:"^[ ]*BIND_HOST[ ]*=" /C:"^[ ]*ADMIN_PORT[ ]*=" .env | ".venv\Scripts\python.exe" scripts\windows_env.py
exit /b %errorlevel%
:updated_environment
".venv\Scripts\python.exe" scripts\update_bootstrap.py
exit /b %errorlevel%
:python_error
echo [error] Install Python 3.11-3.13 x64 (3.11 recommended) and recreate any incompatible virtual environment.
exit /b 1
:help
echo Usage: scripts\start.bat [--help]
echo Requires Python 3.11-3.13 x64 (3.11 recommended) and .env with MASTER_SECRET.
echo Creates .venv, installs changed requirements, then starts the local app.
exit /b 0
