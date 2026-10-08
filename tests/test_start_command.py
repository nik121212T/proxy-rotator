"""Finder restart only terminates this project's server."""

import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS Finder launcher")
ROOT = Path(__file__).resolve().parents[1]


def test_finder_restarts_only_same_project(tmp_path):
    processes = []
    projects = [tmp_path / "local project", tmp_path / "other project"]
    try:
        for project in projects:
            (project / "app").mkdir(parents=True)
            (project / "app" / "__init__.py").touch()
            (project / "app" / "main.py").write_text(
                "import time\nfrom pathlib import Path\nPath('ready').touch()\ntime.sleep(60)\n"
            )
            processes.append(subprocess.Popen([sys.executable, "-m", "app.main"], cwd=project))  # noqa: S603 - isolated synthetic module
        for project in projects:
            deadline = time.monotonic() + 5
            while not (project / "ready").exists():
                assert time.monotonic() < deadline
                time.sleep(0.01)
        local = projects[0]
        shutil.copy2(ROOT / "start.command", local)
        (local / "scripts").mkdir()
        (local / "scripts" / "start.sh").write_text("#!/bin/bash\ntouch relaunched\n")
        result = subprocess.run(  # noqa: S603 - copied launcher in an isolated test directory
            ["/bin/bash", "start.command"], cwd=local, capture_output=True, timeout=20
        )
        assert result.returncode == 0, result.stderr
        assert processes[0].poll() is not None
        assert processes[1].poll() is None
        assert (local / "relaunched").exists()
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
