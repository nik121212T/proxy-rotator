"""Exercise shell orchestration without contacting external upstreams."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="POSIX smoke script; Windows launcher tested separately"
)
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "ips,expected",
    [
        ("203.0.113.1,203.0.113.2,203.0.113.1", 0),
        ("203.0.113.1,203.0.113.1,203.0.113.1", 1),
        ("not-an-ip,203.0.113.2,203.0.113.3", 1),
    ],
)
def test_smoke_rotation_and_cleanup(tmp_path: Path, ips: str, expected: int) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy2(ROOT / "scripts" / "smoke_gateway.sh", scripts / "smoke_gateway.sh")
    binary = tmp_path / "bin"
    binary.mkdir()
    venv = tmp_path / ".venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").symlink_to(sys.executable)
    (binary / "python3").symlink_to(sys.executable)
    (tmp_path / ".env").write_text("MASTER_SECRET=synthetic\n")
    start = scripts / "start.sh"
    start.write_text("#!/bin/sh\nexec .venv/bin/python fake_server.py\n")
    start.chmod(0o755)
    (tmp_path / "fake_server.py").write_text("""import os,signal,time
from pathlib import Path
Path('server.pid').write_text(str(os.getpid()))
def stop(*args):
    Path('stopped').touch()
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
while True: time.sleep(.1)
""")
    curl = binary / "curl"
    curl.write_text("""#!/usr/bin/env python3
import json,os,sys,time
from pathlib import Path
args = sys.argv[1:]
if any('/healthz' in a for a in args):
    marker=Path('health-called')
    if not marker.exists():
        marker.touch()
        raise SystemExit(7)
    for _ in range(100):
        if Path('server.pid').exists(): break
        time.sleep(.01)
    print('200',end='')
elif any('/api/pool/stats' in a for a in args):
    print(json.dumps({'alive':2}))
else:
    assert '--noproxy' in args and args[args.index('--noproxy')+1] == ''
    counter=Path('ip-count')
    n=int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(n+1))
    print(os.environ['FAKE_IPS'].split(',')[n],end='')
""")
    curl.chmod(0o755)
    env = dict(os.environ, PATH=str(binary) + os.pathsep + os.environ["PATH"], FAKE_IPS=ips)
    bash = shutil.which("bash")
    assert bash is not None
    result = subprocess.run(  # noqa: S603 — fixed script in isolated fixture
        [bash, str(scripts / "smoke_gateway.sh")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == expected, result.stdout + result.stderr
    assert (tmp_path / "stopped").exists()
    assert "not-an-ip" not in result.stdout
    assert ("SUCCESS" in result.stdout) == (expected == 0)
