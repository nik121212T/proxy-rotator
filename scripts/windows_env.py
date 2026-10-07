"""Consume only findstr-filtered bindings without evaluating them in cmd.exe."""

import os
import shlex
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    sys.path.insert(0, str(root))
    try:
        for line in sys.stdin:
            key, separator, value = line.partition("=")
            if not separator or key.strip() not in {"BIND_HOST", "ADMIN_PORT"}:
                continue
            parts = shlex.split(value, comments=True)
            if len(parts) != 1:
                raise ValueError("invalid binding")
            os.environ[key.strip()] = parts[0]
    except ValueError:
        sys.stderr.write("[error] Invalid BIND_HOST/ADMIN_PORT in .env.\n")
        return 78
    from app.main import run

    return run()


if __name__ == "__main__":
    raise SystemExit(main())
