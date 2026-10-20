import asyncio
import sys

import pytest


async def test_timeout_kills_descendants(tmp_path):
    from app.update_install import run

    marker = tmp_path / "survived"
    child = "import time,pathlib; time.sleep(1); pathlib.Path('survived').touch()"
    parent = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child!r}]); time.sleep(10)"
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.2):
            await run([sys.executable, "-c", parent], tmp_path)
    await asyncio.sleep(1.2)
    assert not marker.exists()


def test_environment_launcher_validates_commit_and_path(tmp_path):
    from scripts.update_bootstrap import resolve_python

    with pytest.raises(ValueError):
        resolve_python(tmp_path, {"sha": "abc", "environment": "../../elsewhere"}, "abc")
    with pytest.raises(ValueError):
        resolve_python(tmp_path, {"sha": "abc", "environment": ".updates/test/venv"}, "different")


@pytest.mark.parametrize("failure", ["pip", "activation", None])
async def test_preparation_never_changes_live_dependencies_and_rolls_back(
    tmp_path, monkeypatch, failure
):
    import json

    from app import update_install as updater

    root = tmp_path
    (root / ".venv").mkdir()
    live = root / ".venv/live-marker"
    live.write_text("original-dependencies")
    state = {"head": "a" * 40}
    target = "b" * 40
    calls = []

    async def run(args, cwd):
        calls.append(args)
        if args[:3] == ["git", "remote", "get-url"]:
            return "https://github.com/owner/repo.git"
        if args[:2] == ["git", "status"]:
            return ""
        if args[:2] == ["git", "branch"]:
            return "main"
        if args[:2] == ["git", "rev-parse"]:
            return state["head"] if args[2] == "HEAD" else target
        if args[:2] == ["git", "clone"]:
            source = updater.Path(args[-1])
            (source / "scripts").mkdir(parents=True)
            (source / "scripts/update_bootstrap.py").touch()
        if "pip" in args and "install" in args and failure == "pip":
            raise updater.UpdateError("synthetic failure")
        if args[:2] == ["git", "merge"]:
            state["head"] = target
        if args[:2] == ["git", "reset"]:
            state["head"] = args[-1]
        return ""

    monkeypatch.setattr(updater, "run", run)
    original = updater.atomic_write

    def write(path, data):
        if failure == "activation":
            raise OSError("synthetic failure")
        original(path, data)

    monkeypatch.setattr(updater, "atomic_write", write)
    if failure:
        with pytest.raises((updater.UpdateError, OSError)):
            await updater.install(root, "owner/repo", "v9.9.9")
        assert state["head"] == "a" * 40
        assert not (root / ".updates/active.json").exists()
    else:
        await updater.install(root, "owner/repo", "v9.9.9")
        assert state["head"] == target
        assert json.loads((root / ".updates/active.json").read_text())["sha"] == target
    assert live.read_text() == "original-dependencies"
    pip_calls = [call for call in calls if "pip" in call]
    assert all(".updates" in call[0] and ".venv" not in call[0] for call in pip_calls)
    assert ["git", "fetch", "--no-tags", "origin", "refs/tags/v9.9.9"] in calls


async def test_real_git_installs_tag_not_newer_branch_tip(tmp_path, monkeypatch):
    from app import update_install as updater

    producer = tmp_path / "producer"
    producer.mkdir()
    actual_run = updater.run

    async def git(*args, cwd=producer):
        return await actual_run(["git", *args], cwd)

    await git("init", "-b", "main")
    await git("config", "user.name", "Test")
    await git("config", "user.email", "test@example.invalid")
    (producer / ".gitignore").write_text(".updates/\n")
    (producer / "requirements.txt").write_text("")
    (producer / "app").mkdir()
    (producer / "app/__init__.py").write_text('__version__="0.0.1"\n')
    (producer / "app/main.py").write_text("")
    (producer / "scripts").mkdir()
    (producer / "scripts/update_bootstrap.py").write_text("")
    await git("add", ".")
    await git("commit", "-m", "old")
    old = await git("rev-parse", "HEAD")
    (producer / "app/__init__.py").write_text('__version__="9.9.9"\n')
    await git("add", ".")
    await git("commit", "-m", "release")
    target = await git("rev-parse", "HEAD")
    await git("tag", "v9.9.9")
    (producer / "unreleased.txt").write_text("not released")
    await git("add", ".")
    await git("commit", "-m", "unreleased")
    client = tmp_path / "client"
    await git("clone", str(producer), str(client))
    await git("reset", "--hard", old, cwd=client)

    async def run(args, cwd):
        if args[:3] == ["git", "remote", "get-url"]:
            return "https://github.com/owner/repo.git"
        return await actual_run(args, cwd)

    monkeypatch.setattr(updater, "run", run)
    await updater.install(client, "owner/repo", "v9.9.9")
    assert await git("rev-parse", "HEAD", cwd=client) == target
    assert not (client / "unreleased.txt").exists()
    assert (client / ".updates/active.json").exists()


def test_update_children_do_not_inherit_application_secrets(monkeypatch):
    from app.update_install import safe_env

    monkeypatch.setenv("MASTER_SECRET", "must-not-leak")
    monkeypatch.setenv("UPDATES_GITHUB_TOKEN", "must-not-leak")
    monkeypatch.setenv("PROVIDER_PASSWORD", "must-not-leak")
    environment = safe_env()
    assert "must-not-leak" not in environment.values()
    assert environment["GIT_TERMINAL_PROMPT"] == "0"


@pytest.mark.parametrize("payload", [[], None, {"sha": "abc", "environment": 12}])
def test_launcher_rejects_malformed_pointer(tmp_path, payload):
    from scripts.update_bootstrap import resolve_python

    with pytest.raises(ValueError):
        resolve_python(tmp_path, payload, "abc")
