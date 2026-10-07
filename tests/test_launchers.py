"""Bootstrap subprocess tests in isolated project copies, with no package downloads."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def launcher_project(tmp_path: Path) -> Path:
    project = tmp_path / "project with spaces"
    (project / "scripts").mkdir(parents=True)
    for name in ("start.sh", "start.bat"):
        shutil.copy2(ROOT / "scripts" / name, project / "scripts" / name)
    (project / "requirements.txt").write_text("# empty fixture requirements\n")
    # Exercise pip invocation/error handling without downloading packages.
    (project / "pip.py").write_text("""import os
from pathlib import Path
p=Path('installs')
p.write_text(str(int(p.read_text())+1) if p.exists() else '1')
raise SystemExit(int(os.environ.get('TEST_PIP_EXIT','0')))
""")
    (project / "app").mkdir()
    (project / "app" / "__init__.py").touch()
    (project / "app" / "main.py").write_text("from pathlib import Path\nPath('launched').touch()\n")
    return project


def unix_launch(
    project: Path, *args: str, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    bash = shutil.which("bash")
    assert bash is not None
    binary = project / "bin"
    binary.mkdir(exist_ok=True)
    python = binary / "python3"
    if not python.exists():
        python.symlink_to(sys.executable)
    env = dict(os.environ, PATH=str(binary) + os.pathsep + os.environ["PATH"])
    env.update(extra_env or {})
    return subprocess.run(  # noqa: S603 — isolated launcher, no shell input
        [bash, str(project / "scripts" / "start.sh"), *args],
        cwd=project.parent,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX launcher")
def test_unix_help_works_without_env_or_dependencies(launcher_project: Path) -> None:
    result = unix_launch(launcher_project, "--help")
    assert result.returncode == 0
    assert "Usage" in result.stdout
    assert not (launcher_project / ".venv").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX launcher")
def test_unix_bootstrap_updates_only_outdated_dependencies(launcher_project: Path) -> None:
    (launcher_project / ".env").write_text(
        "MASTER_SECRET=synthetic\nBIND_HOST=127.0.0.1\nADMIN_PORT=8080\n"
    )
    result = unix_launch(launcher_project)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (launcher_project / "launched").exists()
    assert (launcher_project / "data").is_dir()
    assert (launcher_project / "installs").read_text() == "1"
    assert unix_launch(launcher_project).returncode == 0
    assert (launcher_project / "installs").read_text() == "1"
    stamp = launcher_project / ".venv" / ".deps-installed"
    os.utime(stamp, (1, 1))
    assert unix_launch(launcher_project).returncode == 0
    assert (launcher_project / "installs").read_text() == "2"


@pytest.mark.skipif(os.name == "nt", reason="POSIX launcher")
def test_unix_missing_env_and_failed_install_do_not_launch(launcher_project: Path) -> None:
    result = unix_launch(launcher_project)
    assert result.returncode == 78
    assert not (launcher_project / "launched").exists()
    (launcher_project / ".env").write_text("MASTER_SECRET=synthetic\n")
    result = unix_launch(launcher_project, extra_env={"TEST_PIP_EXIT": "1"})
    assert result.returncode != 0
    assert not (launcher_project / ".venv" / ".deps-installed").exists()
    assert not (launcher_project / "launched").exists()


@pytest.mark.parametrize("bindings", ['BIND_HOST="127.0.0.2"\nADMIN_PORT=8091 # admin\n', ""])
def test_windows_filtered_bindings_start_application(launcher_project: Path, bindings: str) -> None:
    import json

    helper = ROOT / "scripts" / "windows_env.py"
    assert helper.exists(), "Windows filtered-env entry point is missing"
    shutil.copy2(helper, launcher_project / "scripts" / helper.name)
    (launcher_project / "app" / "main.py").write_text(
        "import os, json\nfrom pathlib import Path\n"
        "def run():\n"
        '    Path("bindings.json").write_text(json.dumps({k: os.getenv(k) for k in '
        '("BIND_HOST", "ADMIN_PORT")}))\n'
        "    return 0\n"
    )
    env = {k: v for k, v in os.environ.items() if k not in {"BIND_HOST", "ADMIN_PORT"}}
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(launcher_project / "scripts" / helper.name)],
        cwd=launcher_project.parent,
        env=env,
        input=bindings,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    actual = json.loads((launcher_project / "bindings.json").read_text())
    assert actual == {
        "BIND_HOST": "127.0.0.2" if bindings else None,
        "ADMIN_PORT": "8091" if bindings else None,
    }


@pytest.mark.skipif(os.name != "nt", reason="Native Windows launcher; CI windows-latest")
def test_windows_bootstrap_and_dependency_refresh(launcher_project: Path) -> None:
    shutil.copy2(ROOT / "scripts" / "windows_env.py", launcher_project / "scripts")
    (launcher_project / "app" / "main.py").write_text(
        'from pathlib import Path\ndef run():\n    Path("launched").touch()\n    return 0\n'
    )

    command = shutil.which("cmd.exe")
    assert command is not None

    def launch(*args: str, fail_pip: bool = False) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603
            [command, "/d", "/c", "scripts\\start.bat", *args],
            cwd=launcher_project,
            text=True,
            capture_output=True,
            timeout=60,
            env=dict(os.environ, TEST_PIP_EXIT="1" if fail_pip else "0"),
        )

    assert launch("--help").returncode == 0
    assert not (launcher_project / ".venv").exists()
    assert launch().returncode == 78
    (launcher_project / ".env").write_text("MASTER_SECRET=synthetic\n")
    assert launch(fail_pip=True).returncode != 0
    stamp = launcher_project / ".venv" / ".deps-installed"
    assert not stamp.exists()
    result = launch()
    assert result.returncode == 0, result.stdout + result.stderr
    assert (launcher_project / "launched").exists()
    assert (launcher_project / "data").is_dir()
    assert (launcher_project / "installs").read_text() == "2"
    assert launch().returncode == 0
    assert (launcher_project / "installs").read_text() == "2"
    os.utime(stamp, (1, 1))
    assert launch().returncode == 0
    assert (launcher_project / "installs").read_text() == "3"
