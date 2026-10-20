import subprocess
import sys
from pathlib import Path

import pytest

from scripts.windows_bootstrap import ensure_dependencies, ensure_env


def test_configuration_generated_once_without_exposing_secret(tmp_path, capsys):
    (tmp_path / ".env.example").write_text("MASTER_SECRET=CHANGE_ME_NOW\nADMIN_PORT=8080\n")
    ensure_env(tmp_path)
    first = (tmp_path / ".env").read_text()
    assert "CHANGE_ME_NOW" not in first
    assert "ADMIN_PORT=8080" in first
    assert first.splitlines()[0].split("=", 1)[1] not in capsys.readouterr().out
    ensure_env(tmp_path)
    assert (tmp_path / ".env").read_text() == first


def test_existing_configuration_and_data_are_not_rekeyed(tmp_path):
    (tmp_path / ".env").write_text("MASTER_SECRET=existing-key\nADMIN_PORT=5001\n")
    ensure_env(tmp_path)
    assert (tmp_path / ".env").read_text() == "MASTER_SECRET=existing-key\nADMIN_PORT=5001\n"


def test_missing_config_with_existing_database_stops(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data/proxies.db").write_bytes(b"existing encrypted data")
    (tmp_path / ".env.example").write_text("MASTER_SECRET=CHANGE_ME_NOW\n")
    with pytest.raises(RuntimeError, match="configuration"):
        ensure_env(tmp_path)
    assert not (tmp_path / ".env").exists()


def test_dependencies_checked_each_launch_and_missing_package_repaired(tmp_path):
    # Real pip invoked with a local wheel, no network and no mocks of metadata.
    import base64
    import csv
    import hashlib
    import io
    import zipfile

    venv = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)  # noqa: S603
    python = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    wheel = tmp_path / "bootstrap_fixture-1.0-py3-none-any.whl"
    entries = {
        "bootstrap_fixture.py": b"VALUE = 1\n",
        "bootstrap_fixture-1.0.dist-info/METADATA": b"Metadata-Version: 2.1\nName: bootstrap-fixture\nVersion: 1.0\n",
        "bootstrap_fixture-1.0.dist-info/WHEEL": b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    rows = []
    for name, content in entries.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode().rstrip("=")
        rows.append((name, "sha256=" + digest, str(len(content))))
    rows.append(("bootstrap_fixture-1.0.dist-info/RECORD", "", ""))
    record = io.StringIO()
    csv.writer(record).writerows(rows)
    entries[rows[-1][0]] = record.getvalue().encode()
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    (tmp_path / "requirements.txt").write_text(
        "--no-index\n--find-links .\nbootstrap-fixture==1.0\n"
    )
    ensure_dependencies(tmp_path, python)
    ensure_dependencies(tmp_path, python)
    subprocess.run([str(python), "-m", "pip", "uninstall", "-y", "bootstrap-fixture"], check=True)  # noqa: S603
    ensure_dependencies(tmp_path, python)
    check = subprocess.run([str(python), "-c", "import bootstrap_fixture"], cwd=tmp_path.parent)  # noqa: S603
    assert check.returncode == 0


def test_launcher_lock_rejects_concurrent_run_and_releases(tmp_path):
    from scripts.windows_bootstrap import launch_lock

    with launch_lock(tmp_path), pytest.raises(RuntimeError, match="deja actif"):  # noqa: SIM117
        with launch_lock(tmp_path):
            pytest.fail("Concurrent bootstrap acquired the lock")
    with launch_lock(tmp_path):
        pass


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows PowerShell")
def test_powershell_detects_existing_python_without_installing():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(  # noqa: S603,S607
        [  # noqa: S607 - Windows system executable
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(root / "scripts/bootstrap_windows.ps1"),
            "-CheckOnly",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Python x64 compatible disponible" in result.stdout
    assert "Telechargement" not in result.stdout
