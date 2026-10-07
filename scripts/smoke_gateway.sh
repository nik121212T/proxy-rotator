#!/usr/bin/env bash
# Live integration proof. Never run this in CI: it needs paid upstream proxies.
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077
smoke_log=/tmp/proxy-rotator-smoke.log
server_pid=""

fail() { printf '[FAIL] %s\n' "$1" >&2; exit 1; }
cleanup() {
  if [[ -n "$server_pid" ]]; then
    kill -TERM -- "-$server_pid" 2>/dev/null || kill -TERM "$server_pid" 2>/dev/null || true
    for ((attempt=0; attempt<50; attempt++)); do
      kill -0 -- "-$server_pid" 2>/dev/null || break
      sleep 0.1
    done
    if kill -0 -- "-$server_pid" 2>/dev/null; then
      kill -KILL -- "-$server_pid" 2>/dev/null || true
    fi
    wait "$server_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT
trap 'exit 1' INT TERM
command -v python3 >/dev/null 2>&1 || fail 'python3 is required.'
command -v curl >/dev/null 2>&1 || fail 'curl is required.'
[[ -x .venv/bin/python ]] || fail 'Virtual environment missing. Run ./scripts/start.sh first.'
[[ -f .env ]] || fail 'Missing .env: configure MASTER_SECRET and import live upstream proxies first.'
[[ -x scripts/start.sh ]] || fail 'scripts/start.sh is missing or not executable.'
if curl -q -s --noproxy '*' --max-time 1 -o /dev/null http://127.0.0.1:8080/healthz; then
  fail 'Port 8080 already serves HTTP. Stop the existing app before this smoke test.'
fi
[[ ! -L "$smoke_log" ]] || fail 'Refusing a symlink at the smoke log path.'
: > "$smoke_log"
chmod 600 "$smoke_log"
# Own a separate process group, including dependency-install children if any.
.venv/bin/python -c 'import os; os.setsid(); os.execv("./scripts/start.sh", ["./scripts/start.sh"])' > "$smoke_log" 2>&1 &
server_pid=$!
printf '[smoke] Waiting for admin readiness (30s maximum)…\n'
ready=false
deadline=$((SECONDS + 30))
while ((SECONDS < deadline)); do
  kill -0 "$server_pid" 2>/dev/null || fail "Server exited; inspect $smoke_log (not echoed to protect secrets)."
  code=$(curl -q -s --noproxy '*' --max-time 1 -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/healthz || true)
  if [[ "$code" == 200 ]]; then ready=true; break; fi
  sleep 1
done
[[ "$ready" == true ]] || fail "Admin not ready after 30s; inspect $smoke_log."
printf '[smoke] Waiting for at least two alive proxies (60s maximum)…\n'
ready=false
deadline=$((SECONDS + 60))
while ((SECONDS < deadline)); do
  kill -0 "$server_pid" 2>/dev/null || fail "Server exited; inspect $smoke_log."
  stats=$(curl -q -fs --noproxy '*' --max-time 1 http://127.0.0.1:8080/api/pool/stats || true)
  if printf '%s' "$stats" | .venv/bin/python -c '
import json,sys
try:
    alive=json.load(sys.stdin).get("alive", 0)
    sys.exit(0 if isinstance(alive, int) and alive >= 2 else 1)
except (ValueError, AttributeError):
    sys.exit(1)
'; then ready=true; break; fi
  sleep 1
done
[[ "$ready" == true ]] || fail 'Pool needs >=2 healthy upstream proxies. Import them via the UI and retry.'
ips=()
for attempt in 1 2 3; do
  raw=$(curl -q -fs --noproxy '' --max-time 10 -x http://127.0.0.1:8888 https://api.ipify.org) || fail "Request $attempt failed through the HTTP gateway."
  ip=$(printf '%s' "$raw" | .venv/bin/python -c '
import ipaddress,sys
try:
    print(ipaddress.ip_address(sys.stdin.read().strip()))
except ValueError:
    sys.exit(1)
') || fail "Request $attempt returned an invalid IP (response body withheld)."
  ips+=("$ip")
  printf '[smoke] Request %s: %s\n' "$attempt" "$ip"
done
if [[ "${ips[0]}" == "${ips[1]}" && "${ips[1]}" == "${ips[2]}" ]]; then
  fail 'All three requests returned the same IP. Use round_robin and proxies with distinct exit IPs.'
fi
printf '[SUCCESS] Three-rotation proof: at least two distinct exit IPs across three requests.\n'
