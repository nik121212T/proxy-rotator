#!/usr/bin/env bash
# Self-update entry point used by POST /api/updates/apply.
# Safety rails:
#   - must be in a git working tree,
#   - origin must point at the sanctioned repo (strict owner/repo match on GitHub),
#   - working tree must be clean (no local edits),
#   - HEAD must not be detached,
#   - fast-forward only (no merge commits, no rewritten history),
#   - pip install runs against the project's own venv,
#   - if pip install fails after the merge, the working tree is reset to
#     the previous SHA so the user is never stranded on a broken state.
set -euo pipefail

cd "$(dirname "$0")/.."

EXPECTED_REPO="${PROXY_ROTATOR_UPDATE_REPO:-nik121212T/proxy-rotator}"

log() { printf '[update] %s\n' "$1"; }
fail() { printf '[update:FAIL] %s\n' "$1" >&2; exit 1; }

command -v git >/dev/null 2>&1 || fail "git is required"
command -v python3 >/dev/null 2>&1 || fail "python3 is required"
[[ -d .git ]] || fail "Not a git working tree"

# Strict allow-list: origin must be github.com and end with the exact
# owner/repo (optionally .git). Reject near-name forks like
# `evil/nik121212T/proxy-rotator-backdoor.git` or hosts other than GitHub.
remote_url="$(git remote get-url origin 2>/dev/null || true)"
if [[ -z "$remote_url" ]]; then
  fail "origin remote is not configured"
fi
if ! [[ "$remote_url" =~ ^(https://github\.com/|git@github\.com:)${EXPECTED_REPO}(\.git)?$ ]]; then
  fail "origin does not point at github.com:${EXPECTED_REPO} (got: $remote_url)"
fi

if ! git diff --quiet || ! git diff --cached --quiet; then
  fail "Working tree has local changes; stash or commit them before updating."
fi

branch="$(git rev-parse --abbrev-ref HEAD)"
if [[ "$branch" == "HEAD" ]]; then
  fail "HEAD is detached; check out a branch (e.g. 'git checkout main') before updating."
fi
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

# Rollback rail: if pip install fails after the merge, reset the working
# tree to the previous SHA so the venv and the repo stay consistent.
rollback() {
  log "Install failed — rolling back repo to $local_sha"
  git reset --hard "$local_sha" || log "Rollback failed; repo may be inconsistent."
}

if [[ -x .venv/bin/pip ]]; then
  log "Updating Python dependencies"
  if ! .venv/bin/pip install --disable-pip-version-check -r requirements.txt; then
    rollback
    fail "pip install failed; repo reset to $local_sha. Fix the environment, then retry."
  fi
else
  # No venv? If requirements.txt changed in this update, we have no safe way
  # to install the new deps — refuse rather than exit 0 with the user ending
  # up on new code that imports packages their system Python does not have.
  if ! git diff --quiet "$local_sha" "$remote_sha" -- requirements.txt; then
    rollback
    fail "requirements.txt changed but no .venv/bin/pip was found; install deps manually or run scripts/install.sh."
  fi
  log "No .venv/bin/pip detected and no requirements change; nothing to install."
fi

log "Update complete. Relaunch the app to use the new version."
