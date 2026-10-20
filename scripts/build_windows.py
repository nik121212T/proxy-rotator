"""Build a clean source distribution from an explicit application-file allowlist."""

import argparse
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT_FILES = (
    ".env.example", "requirements.txt", "pyproject.toml", "README.md",
    "ARCHITECTURE.md", "SPEC.md", "CHANGELOG.md", "Lancer.bat",
)


def build(root: Path, output: Path) -> None:
    files = [root / name for name in ROOT_FILES]
    for folder, suffixes in (
        ("app", {".py", ".html", ".css", ".js", ".svg", ".woff2", ".ico", ".txt", ".md"}),
        ("scripts", {".py", ".bat", ".sh", ".ps1"}),
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
            "1. Extrayez TOUT le ZIP dans un dossier personnel (ex. Documents).\n"
            "2. Double-cliquez sur Lancer.bat a la racine du dossier.\n"
            "3. Le lanceur verifie/installe Python et les dependances, cree .env\n"
            "   avec une cle unique puis ouvre le navigateur automatiquement.\n"
            "4. Gardez la console ouverte pendant l'utilisation.\n"
            "5. Importez vos proxies dans votre propre installation.\n\n"
            "Internet est necessaire au premier lancement et pour les reparations.\n"
            "Les moteurs de navigateurs restent telechargeables depuis l'application.\n"
            "Ne partagez pas votre .env : il contient votre cle de chiffrement.\n"
            "Ne lancez pas l'application directement depuis le ZIP.\n"
            "En cas d'erreur, la fenetre reste ouverte : relevez la derniere etape.\n"
            "Consultez docs/WINDOWS.md. Aucun proxy, mot de passe ou profil personnel inclus.\n"
        ))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("dist/proxy-rotator-windows.zip"))
    args = parser.parse_args()
    build(Path(__file__).resolve().parents[1], args.output.resolve())
