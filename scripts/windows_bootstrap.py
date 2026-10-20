"""Prepare a Windows installation; never print or replace an existing secret."""

from __future__ import annotations

import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import urllib.request
import uuid
import webbrowser
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def step(message: str) -> None:
    print(message, flush=True)


def run(args: list[str], root: Path, *, quiet: bool = False) -> int:
    return subprocess.run(  # noqa: S603 - fixed executables and argument arrays
        args,
        cwd=root,
        timeout=1200,
        stdout=subprocess.DEVNULL if quiet else None,
        stderr=subprocess.DEVNULL if quiet else None,
    ).returncode


@contextmanager
def launch_lock(root: Path) -> Iterator[None]:
    with (root / ".bootstrap.lock").open("a+b") as lock:
        lock.seek(0)
        if lock.seek(0, 2) == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError("Un autre lanceur est deja actif pour ce dossier.") from None
        yield


def ensure_env(root: Path) -> None:
    target = root / ".env"
    existing = target.exists()
    text = (
        target.read_text(encoding="utf-8-sig")
        if existing
        else (root / ".env.example").read_text(encoding="utf-8-sig")
    )
    match = re.search(r"^MASTER_SECRET\s*=\s*(.*?)\s*$", text, re.MULTILINE)
    secret = match.group(1).strip("'\"") if match else ""
    if existing and secret and secret != "CHANGE_ME_NOW":  # noqa: S105 - template placeholder
        step("[2/6] Configuration .env existante conservee.")
        return
    if (root / "data/proxies.db").exists():
        raise RuntimeError(
            "Base existante : restaurez sa configuration .env originale avant de lancer."
        )
    replacement = "MASTER_SECRET=" + secrets.token_urlsafe(48)
    if match:
        text = text[: match.start()] + replacement + text[match.end() :]
    else:
        text += "\n" + replacement + "\n"
    if existing:
        # Only the unconfigured placeholder is replaced; all other settings stay intact.
        target.write_text(text, encoding="utf-8")
    else:
        with target.open("x", encoding="utf-8") as stream:
            stream.write(text)
    target.chmod(0o600)
    step("[2/6] Configuration .env creee avec une cle unique (non affichee).")


def ensure_environment(root: Path) -> Path:
    if (root / ".updates/active.json").exists():
        import json

        from scripts.update_bootstrap import resolve_python

        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()  # noqa: S603,S607
        return resolve_python(root, json.loads((root / ".updates/active.json").read_text()), head)
    environment = root / ".venv"
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    probe = 'import sys,struct; sys.exit(0 if (3,11)<=sys.version_info<(3,14) and struct.calcsize("P")==8 else 1)'
    try:
        valid = python.is_file() and run([str(python), "-c", probe], root, quiet=True) == 0
    except OSError:
        valid = False
    if valid:
        step("[3/6] Environnement Python existant valide.")
        return python
    if environment.exists():
        environment.rename(root / (".venv.backup-" + uuid.uuid4().hex[:8]))
        step("[3/6] Ancien environnement incompatible conserve en sauvegarde.")
    step("[3/6] Creation de l’environnement Python du projet...")
    if run([sys.executable, "-m", "venv", str(environment)], root):
        raise RuntimeError(
            "Creation de l’environnement impossible. Verifiez espace disque et droits."
        )
    return python


def ensure_dependencies(root: Path, python: Path) -> None:
    step("[4/6] Verification de pip et des dependances (y compris les options)...")
    if run([str(python), "-m", "pip", "--version"], root, quiet=True) and run(
        [str(python), "-m", "ensurepip", "--upgrade"], root
    ):
        raise RuntimeError("Impossible de reparer pip.")
    # Older bundled pip versions do not understand modern wheel tags (abi3,
    # per-Python-version builds) and then fail to find legitimate releases
    # with messages like "Ignoring the following versions that require
    # Python 3.14". Always upgrade pip + packaging first so resolution uses
    # the current wheel-tag table.
    step("[4/6] Mise a jour de pip pour reconnaitre les wheels recentes...")
    if run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--quiet",
            "--disable-pip-version-check",
            "--upgrade",
            "pip",
            "setuptools",
            "wheel",
        ],
        root,
    ):
        raise RuntimeError(
            "Mise a jour de pip impossible. Verifiez Internet et les droits du dossier."
        )
    check = [
        str(python),
        str(ROOT / "scripts/check_dependencies.py"),
        str(root / "requirements.txt"),
    ]
    if run(check, root, quiet=True) or run([str(python), "-m", "pip", "check"], root, quiet=True):
        step("[4/6] Installation/reparation des dependances. Details ci-dessous :")
        base_args = [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--progress-bar",
            "on",
            "-r",
            str(root / "requirements.txt"),
        ]
        if run(base_args, root):
            # Retry forcing the public PyPI index — defeats a corporate or
            # cached mirror that may not carry the pinned versions.
            step(
                "[4/6] Nouvelle tentative via pypi.org (contournement d'un miroir local)..."
            )
            if run([*base_args, "--index-url", "https://pypi.org/simple/"], root):
                raise RuntimeError(
                    "Installation interrompue. Verifiez Internet puis relancez Lancer.bat."
                )
        if run(check, root) or run([str(python), "-m", "pip", "check"], root):
            raise RuntimeError("Dependances toujours incompatibles : application non demarree.")
    (python.parent.parent / ".deps-installed").touch()
    step("[4/6] Dependances valides.")


def serve() -> int:
    # Executed inside the prepared venv, where configuration dependencies exist.
    from app.main import load_settings
    from app.main import run as run_app

    config = load_settings()
    family = socket.AF_INET6 if ":" in config.bind_host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((config.bind_host, config.admin_port))
        except OSError:
            step(
                f"[ERREUR] Le port {config.admin_port} est occupe. Fermez l’autre instance puis relancez."
            )
            return 1
    host = (
        "127.0.0.1"
        if config.bind_host in {"0.0.0.0", "127.0.0.1", "localhost"}  # noqa: S104
        else config.bind_host
    )  # noqa: S104 - URL mapping only
    if ":" in host:
        host = "[" + host + "]"
    base = f"http://{host}:{config.admin_port}"
    stop = threading.Event()

    def open_when_ready() -> None:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for _ in range(120):
            if stop.is_set():
                return
            try:
                with opener.open(base + "/healthz", timeout=1) as response:  # noqa: S310 - local configured app
                    if response.status == 200:
                        step("[6/6] Application prete : " + base)
                        webbrowser.open(base + ("/login" if config.admin_token else "/"))
                        return
            except OSError:
                pass
            stop.wait(0.5)
        step("[INFO] Demarrage en cours ; ouvrez manuellement " + base)

    thread = threading.Thread(target=open_when_ready, daemon=True)
    step("[5/6] Demarrage du serveur. Gardez cette fenetre ouverte.")
    thread.start()
    try:
        return run_app()
    finally:
        stop.set()
        thread.join(timeout=2)


def main() -> int:
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    try:
        if "--serve" in sys.argv:
            return serve()
        with launch_lock(ROOT):
            ensure_env(ROOT)
            python = ensure_environment(ROOT)
            ensure_dependencies(ROOT, python)
            if run([str(python), "-c", "import app.main"], ROOT, quiet=True):
                step("[4/6] Import impossible : reparation des fichiers des dependances...")
                if run(
                    [
                        str(python),
                        "-m",
                        "pip",
                        "install",
                        "--force-reinstall",
                        "--disable-pip-version-check",
                        "-r",
                        str(ROOT / "requirements.txt"),
                    ],
                    ROOT,
                ):
                    raise RuntimeError("Reparation des dependances impossible.")
                if run([str(python), "-c", "import app.main"], ROOT):
                    raise RuntimeError(
                        "Application incomplete ou incompatible. Re-extrayez le ZIP."
                    )
            if "--prepare-only" in sys.argv:
                return 0
            # The server may run indefinitely; installation commands alone have a timeout.
            return subprocess.call(  # noqa: S603 - known project interpreter
                [str(python), str(Path(__file__).resolve()), "--serve"], cwd=ROOT
            )  # noqa: S603
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        # Configuration errors carry safe field names; no .env contents are logged.
        step("[ERREUR] " + str(error))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
