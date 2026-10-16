"""Source packages must never carry local user data or virtual environments."""

from zipfile import ZipFile

from scripts.build_windows import build


def test_windows_package_excludes_secrets_profiles_and_symlinks(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    for name in (".env", "data/proxies.db", "data/.salt", "data/browser-profiles/session/Cookies",
                 ".venv/site.py", "app/__pycache__/module.py", "app/.private/config.py"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("PRIVATE SENTINEL")
    (root / ".env.example").write_text("MASTER_SECRET=CHANGE_ME_NOW")
    (root / "app/main.py").write_text("# application")
    (root / "app/leaked.py").symlink_to(root / ".env")
    output = tmp_path / "bundle.zip"
    build(root, output)
    with ZipFile(output) as archive:
        assert set(archive.namelist()) == {
            "proxy-rotator/.env.example", "proxy-rotator/app/main.py",
            "proxy-rotator/LIRE-MOI-WINDOWS.txt",
        }
        assert all(b"PRIVATE SENTINEL" not in archive.read(name) for name in archive.namelist())
