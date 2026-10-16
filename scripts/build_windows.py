"""Build a clean source distribution from an explicit application-file allowlist."""

import argparse
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT_FILES = (
    ".env.example", "requirements.txt", "pyproject.toml", "README.md",
    "ARCHITECTURE.md", "SPEC.md", "CHANGELOG.md",
)


def build(root: Path, output: Path) -> None:
    files = [root / name for name in ROOT_FILES]
    for folder, suffixes in (
        ("app", {".py", ".html", ".css", ".js", ".svg", ".woff2", ".ico", ".txt", ".md"}),
        ("scripts", {".py", ".bat", ".sh"}),
        ("docs", {".md"}),
    ):
        files.extend(p for p in (root / folder).rglob("*")
                     if p.suffix in suffixes and not p.is_symlink()
                     and not any(part.startswith(".") or part == "__pycache__"
                                 for part in p.relative_to(root).parts))
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for path in sorted(set(files)):
            if path.is_file() and not path.is_symlink():
                archive.write(path, Path("proxy-rotator") / path.relative_to(root))
        archive.writestr("proxy-rotator/LIRE-MOI-WINDOWS.txt", (
            "1. Installez Python 3.11 x64 (Add python.exe to PATH).\n"
            "2. Extrayez tout le dossier dans votre dossier personnel.\n"
            "3. Copiez .env.example en .env et remplacez MASTER_SECRET=CHANGE_ME_NOW\n"
            "   par une longue phrase secrete propre a cette installation.\n"
            "4. Dans un terminal, lancez scripts\\start.bat.\n"
            "5. Ouvrez http://127.0.0.1:8080 puis importez vos proxies.\n"
            "6. Dans Navigateurs, telechargez les moteurs souhaites.\n\n"
            "Consultez docs/WINDOWS.md pour les prerequis et limites de validation.\n"
            "Cette archive ne contient ni proxies, ni mots de passe, ni profils.\n"
        ))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("dist/proxy-rotator-windows.zip"))
    args = parser.parse_args()
    build(Path(__file__).resolve().parents[1], args.output.resolve())
