"""Launch the prepared environment without relocating an installed virtualenv."""

import json
import os
import subprocess
from pathlib import Path


def resolve_python(root: Path, payload: object, head: str) -> Path:
    if not isinstance(payload, dict) or not isinstance(payload.get("environment"), str):
        raise ValueError("Configuration de mise à jour invalide.")
    if payload.get("sha") != head:
        raise ValueError("La version du code et l’environnement préparé ne correspondent pas.")
    environment = (root / payload["environment"]).resolve()
    if not environment.is_relative_to((root / ".updates").resolve()) or environment.name != "venv":
        raise ValueError("Chemin de l’environnement de mise à jour invalide.")
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.is_file():
        raise ValueError("Environnement préparé introuvable.")
    return python


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    try:
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()  # noqa: S603,S607
        python = resolve_python(root, json.loads((root / ".updates/active.json").read_text()), head)
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError):
        print(
            "Environnement de mise à jour invalide. Vérifiez .updates/active.json et la version Git."
        )
        return 1
    os.chdir(root)
    os.execv(str(python), [str(python), "-m", "app.main"])  # noqa: S606
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
