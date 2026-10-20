"""Prepare an isolated environment, then fast-forward to an exact release tag."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import signal
import sys
import uuid
from pathlib import Path


class UpdateError(ValueError):
    pass


def safe_env() -> dict[str, str]:
    keys = (
        "PATH",
        "HOME",
        "USER",
        "USERPROFILE",
        "SYSTEMDRIVE",
        "SSH_AUTH_SOCK",
        "GH_CONFIG_DIR",
        "LANG",
        "LC_ALL",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "APPDATA",
        "LOCALAPPDATA",
    )
    return {**{k: os.environ[k] for k in keys if k in os.environ}, "GIT_TERMINAL_PROMPT": "0"}


async def run(args: list[str], root: Path) -> str:
    options: dict[str, object] = {}
    if os.name == "nt":
        options["creationflags"] = 0x00000200  # CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    process = await asyncio.create_subprocess_exec(
        *args,
        cwd=root,
        env=safe_env(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        **options,  # type: ignore[arg-type]
    )
    try:
        output, _ = await process.communicate()
    except BaseException:
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        else:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
        raise
    if process.returncode:
        raise UpdateError(
            f"Échec de {Path(args[0]).name}. Vérifiez les accès Git et les dépendances."
        )
    return output.decode("utf-8", errors="replace").strip()


def atomic_write(path: Path, content: bytes) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


async def install(root: Path, repo: str, tag: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise UpdateError("Dépôt invalide.")
    if not re.fullmatch(r"v?\d+\.\d+\.\d+", tag):
        raise UpdateError("Version stable invalide.")
    remote = await run(["git", "remote", "get-url", "origin"], root)
    if remote not in {
        f"https://github.com/{repo}",
        f"https://github.com/{repo}.git",
        f"git@github.com:{repo}",
        f"git@github.com:{repo}.git",
    }:
        raise UpdateError("Le dépôt origin ne correspond pas au dépôt autorisé.")
    if await run(["git", "status", "--porcelain", "--untracked-files=normal"], root):
        raise UpdateError(
            "Le dossier contient des modifications locales. Enregistrez-les avant de mettre à jour."
        )
    if await run(["git", "branch", "--show-current"], root) != "main":
        raise UpdateError("La mise à jour des releases nécessite la branche main.")
    old_sha = await run(["git", "rev-parse", "HEAD"], root)
    await run(["git", "fetch", "--no-tags", "origin", f"refs/tags/{tag}"], root)
    target = await run(["git", "rev-parse", "FETCH_HEAD^{commit}"], root)
    if target == old_sha:
        return "Cette release est déjà installée."
    await run(["git", "merge-base", "--is-ancestor", old_sha, target], root)
    updates = root / ".updates"
    if updates.is_symlink():
        raise UpdateError("Le dossier .updates ne doit pas être un lien symbolique.")
    updates.mkdir(exist_ok=True)
    stage = updates / uuid.uuid4().hex
    stage.mkdir()
    source = stage / "source"
    environment = stage / "venv"
    try:
        # Never run pip in the live environment. This path remains stable after activation.
        await run(
            ["git", "clone", "--quiet", "--no-hardlinks", "--no-checkout", str(root), str(source)],
            root,
        )
        await run(["git", "checkout", "--quiet", "--detach", target], source)
        if not (source / "scripts" / "update_bootstrap.py").is_file():
            raise UpdateError("Cette release ne prend pas en charge l’installation isolée.")
        await run([sys.executable, "-m", "venv", str(environment)], root)
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        await run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--quiet",
                "--disable-pip-version-check",
                "-r",
                str(source / "requirements.txt"),
            ],
            source,
        )
        await run([str(python), "-m", "pip", "check"], source)
        await run(
            [
                str(python),
                "-c",
                "import app.main,sys; from app import __version__; assert __version__ == sys.argv[1]",
                tag.lstrip("v"),
            ],
            source,
        )
        # Revalidate after the potentially slow installation: do not overwrite edits made meanwhile.
        if await run(["git", "rev-parse", "HEAD"], root) != old_sha or await run(
            ["git", "status", "--porcelain"], root
        ):
            raise UpdateError("Le dépôt a changé pendant la préparation. Mise à jour annulée.")
    except BaseException:
        await asyncio.to_thread(shutil.rmtree, stage, True)
        raise
    pointer = updates / "active.json"
    old_pointer = pointer.read_bytes() if pointer.exists() else None
    committed = False
    try:
        await run(["git", "merge", "--ff-only", target], root)
        atomic_write(
            pointer,
            json.dumps({"sha": target, "environment": str(environment.relative_to(root))}).encode(),
        )
        committed = True
    finally:
        if not committed:
            # Cancellation may arrive after git has advanced HEAD but before it returned.
            head = await run(["git", "rev-parse", "HEAD"], root)
            if head == target:
                await run(["git", "reset", "--merge", old_sha], root)
            if old_pointer is None:
                pointer.unlink(missing_ok=True)
            else:
                atomic_write(pointer, old_pointer)
    return f"Release {tag} installée. Relancez l’application ; l’ancien environnement est conservé."
