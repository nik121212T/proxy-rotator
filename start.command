#!/bin/bash
# macOS Finder launcher: restart only servers running from this project.
set -euo pipefail
cd "$(dirname "$0")"
project_dir="$(pwd -P)"

# Help and invalid arguments must not stop a running application.
if [[ $# -ne 0 ]]; then
  exec /bin/bash ./scripts/start.sh "$@"
fi

is_project_server() {
  local candidate="$1" command_line working_dir state
  command_line="$(/bin/ps -p "$candidate" -o command= 2>/dev/null)" || return 1
  [[ "$command_line" =~ (^|/)([Pp]ython[^\ /]*|uvicorn)(\ |$) ]] || return 1
  [[ "$command_line" =~ (\ -m\ app.main($|\ ))|(\ app.main:app($|\ )) ]] || return 1
  state="$(/bin/ps -p "$candidate" -o stat= 2>/dev/null)" || return 1
  [[ "$state" != *Z* ]] || return 1
  working_dir="$(/usr/sbin/lsof -a -p "$candidate" -d cwd -Fn 2>/dev/null | /usr/bin/sed -n 's/^n//p')" || return 1
  [[ "$working_dir" == "$project_dir" ]]
}

# Check both the process command and its working directory, never just a port.
while read -r server_pid; do
  [[ "$server_pid" =~ ^[0-9]+$ ]] || continue
  if is_project_server "$server_pid"; then
    echo '[restart] Arrêt de l’ancienne instance de Proxy Rotator…'
    kill -TERM "$server_pid" 2>/dev/null || true
    for ((attempt = 0; attempt < 40; attempt++)); do
      is_project_server "$server_pid" || break
      sleep 0.25
    done
    if is_project_server "$server_pid"; then
      echo '[restart] Fermeture forcée après 10 secondes.'
      kill -KILL "$server_pid" 2>/dev/null || true
      sleep 0.25
    fi
  fi
done < <(/bin/ps -u "$(id -u)" -o pid=)

exec /bin/bash ./scripts/start.sh
