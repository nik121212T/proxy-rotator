"""Exercise the launcher with an exported fake git, never Keychain or network."""

import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(os.name != "posix", reason="macOS shell launcher")
@pytest.mark.parametrize("branch", ["main", "fix/windows-browser-sessions-20261016", ""])
def test_push_targets_current_branch_and_rejects_detached_head(tmp_path, branch):
    launcher = Path(__file__).resolve().parents[1] / "push.command"
    calls = tmp_path / "calls"
    env = {
        **os.environ,
        "TEST_BRANCH": branch,
        "TEST_CALLS": str(calls),
        "TEST_LAUNCHER": str(launcher),
    }
    script = """
git() {
  case "$1" in
    branch) printf '%s' "$TEST_BRANCH" ;;
    status) return 0 ;;
    credential-osxkeychain) printf 'password=synthetic-test-only\n' ;;
    remote) return 0 ;;
    -c) printf '%s\n' "$@" > "$TEST_CALLS" ;;
    *) return 99 ;;
  esac
}
export -f git
bash "$TEST_LAUNCHER"
"""
    result = subprocess.run(  # noqa: S603 - fixed local launcher and synthetic Git
        ["/bin/bash", "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
        start_new_session=True,
    )
    assert "synthetic-test-only" not in result.stdout + result.stderr
    if branch:
        assert result.returncode == 0
        assert calls.read_text().splitlines()[-1] == branch
        assert f"branche {branch}" in result.stdout
    else:
        assert result.returncode != 0
        assert not calls.exists()
        assert "branche" in result.stdout
