"""Check pinned requirements and requested extras without network access."""

import importlib.metadata as metadata
import sys
from pathlib import Path


def main() -> int:
    from pip._vendor.packaging.requirements import Requirement

    pending = [
        Requirement(line.split(" #", 1)[0])
        for raw in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()
        if (line := raw.strip()) and not line.startswith(("#", "-"))
    ]
    seen = set()
    while pending:
        requirement = pending.pop()
        if requirement.marker and not requirement.marker.evaluate():
            continue
        key = (requirement.name.lower(), tuple(sorted(requirement.extras)))
        try:
            if not requirement.specifier.contains(metadata.version(requirement.name)):
                return 1
            if key in seen:
                continue
            seen.add(key)
            for raw in metadata.requires(requirement.name) or []:
                child = Requirement(raw)
                if child.marker is None or any(
                    child.marker.evaluate({"extra": extra}) for extra in {"", *requirement.extras}
                ):
                    child.marker = None
                    pending.append(child)
        except metadata.PackageNotFoundError:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
