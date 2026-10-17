#!/usr/bin/env bash
# Self-update entry point used by POST /api/updates/apply.
# Safety rails:
#   - must be in a git working tree,
#   - origin must point at the sanctioned repo,
#   - working tree must be clean (no local edits),
#   - fast-forward only (no merge commits, no rewritten history),
#   - pip install runs against the project's own venv.
set -euo pipefail

cd "$(dirname "$0")/.."

EXPECTED_REPO="${PROXY_ROTATOR_UPDATE_REPO:-nik121212T/proxy-rotator}"

log() { printf '[update] %s\n' "$1"; }
fail() { printf '[update:FAIL] %s\n' "$1" >&2; exit 1; }

command -v git >/dev/null 2>&1 || fail "git is required"
command -v python3 >/dev/null 2>&1 || fail "python3 is required"
[[ -d .git ]] || fail "Not a git working tree"

remote_url="$(git remote get-url origin 2>/dev/null || true)"
case "$remote_url" in
  *"$EXPECTED_REPO"*) : ;;
  *) fail "origin does not point at $EXPECTED_REPO (got: $remote_url)" ;;
esac

if ! git diff --quiet || ! git diff --cached --quiet; then
  fail "Working tree has local changes; stash or commit them before updating."
fi

branch="$(git rev-parse --abbrev-ref HEAD)"
log "On branch $branch; fetching origin"
git fetch --prune origin "$branch"

local_sha="$(git rev-parse HEAD)"
remote_sha="$(git rev-parse "origin/$branch")"
if [[ "$local_sha" == "$remote_sha" ]]; then
  log "Already up to date ($local_sha)."
  exit 0
fi

# Fast-forward only: refuse divergent history.
base="$(git merge-base HEAD "origin/$branch")"
if [[ "$base" != "$local_sha" ]]; then
  fail "History diverged from origin/$branch; resolve manually."
fi

log "Fast-forwarding $local_sha → $remote_sha"
git merge --ff-only "origin/$branch"

if [[ -x .venv/bin/pip ]]; then
  log "Updating Python dependencies"
  .venv/bin/pip install --disable-pip-version-check -r requirements.txt
else
  log "No .venv/bin/pip detected; skip dependency install"
fi

log "Update complete. Relaunch the app to use the new version."
