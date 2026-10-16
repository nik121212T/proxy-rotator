"""Measure and remove only the current Playwright engine's revision folders."""

import os
import re
import shutil
from pathlib import Path


def engine_folders(engine: str, executable: Path) -> tuple[Path, ...]:
    if engine not in {"chromium", "firefox", "webkit"} or not executable.is_absolute():
        raise ValueError("Unknown engine layout")
    for folder in executable.parents:
        match = re.fullmatch(re.escape(engine) + r"-(\d+)", folder.name)
        if match:
            if any(p.is_symlink() for p in (folder, *folder.parents)):
                raise ValueError("Linked engine directory")
            folders = [folder]
            if engine == "chromium":
                folders.append(folder.parent / f"chromium_headless_shell-{match[1]}")
            if any(p.is_symlink() for p in folders):
                raise ValueError("Linked engine directory")
            return tuple(folders)
    raise ValueError("Unknown engine layout")


def installed_size(folders: tuple[Path, ...]) -> int:
    total = 0
    for folder in folders:
        if not folder.exists():
            continue
        def failed(error: OSError) -> None:
            raise error
        for root, _, files in os.walk(folder, followlinks=False, onerror=failed):
            for name in files:
                path = Path(root) / name
                if not path.is_symlink():
                    total += path.stat().st_size
    return total


def delete_engine(folders: tuple[Path, ...]) -> None:
    for folder in folders:
        if folder.is_symlink():
            raise ValueError("Linked engine directory")
        if folder.exists():
            shutil.rmtree(folder)
