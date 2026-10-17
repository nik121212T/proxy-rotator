"""Bounded, credential-free progress for imports owned by the current process."""

import time
from dataclasses import dataclass


@dataclass
class ImportProgress:
    stage: str = "parsing"
    total: int = 0
    completed: int = 0
    failed: int = 0
    updated: float = 0

    def update(self, stage: str, total: int, completed: int, failed: int) -> None:
        self.stage, self.total, self.completed, self.failed = stage, total, completed, failed
        self.updated = time.monotonic()

    def public(self) -> dict[str, str | int]:
        return {
            "stage": self.stage,
            "total": self.total,
            "completed": self.completed,
            "failed": self.failed,
        }


class ImportTracker:
    def __init__(self) -> None:
        self.items: dict[str, ImportProgress] = {}

    def prune(self) -> None:
        now = time.monotonic()
        self.items = {
            key: value
            for key, value in self.items.items()
            if value.stage not in {"done", "error"} or now - value.updated < 600
        }

    def start(self, identifier: str) -> ImportProgress:
        self.prune()
        if identifier in self.items:
            raise ValueError("Cet import a déjà été envoyé.")
        if len(self.items) >= 64:
            finished = next(
                (key for key, value in self.items.items() if value.stage in {"done", "error"}), None
            )
            if finished is None:
                raise ValueError("Trop d’imports en cours. Attendez leur fin.")
            del self.items[finished]
        progress = ImportProgress(updated=time.monotonic())
        self.items[identifier] = progress
        return progress
