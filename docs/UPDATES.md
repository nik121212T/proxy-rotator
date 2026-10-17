# Mises à jour auto (option A : git-based)

Public visé : installations locales de la bêta amis, pas de packaging binaire.

## Chez l'utilisateur

L'ami récupère le projet une seule fois :

```bash
git clone https://github.com/nik121212T/proxy-rotator.git
cd proxy-rotator
./scripts/install.sh    # crée .venv + installe deps
cp .env.example .env    # renseigne MASTER_SECRET
./scripts/start.sh
```

Au démarrage, l'application interroge GitHub Releases (cache 24 h).
Si une nouvelle version est publiée :

- badge **« Mise à jour disponible »** dans la barre du haut
- carte **« Mises à jour »** dans *Paramètres* : version installée,
  dernière disponible, bouton *Mettre à jour*, lien vers les notes.

Un clic sur *Mettre à jour* lance `scripts/update.sh`, qui :

1. refuse si `origin` ne pointe pas sur `nik121212T/proxy-rotator`
2. refuse si le working tree a des modifications locales
3. `git fetch` puis `git merge --ff-only` (jamais de merge divergent)
4. réinstalle `requirements.txt` dans `.venv/`
5. demande à l'utilisateur de relancer l'app

## Côté mainteneur (toi)

Pour pousser une nouvelle version :

```bash
# 1. bump dans app/__init__.py (__version__ = "0.2.0")
# 2. commit + tag
git commit -am "release: v0.2.0"
git tag v0.2.0
git push --tags origin main
# 3. GitHub → Releases → Draft new release → tag v0.2.0 → notes → Publish
```

Dans la minute, les instances connectées voient la nouvelle version à leur
prochain `GET /api/updates/check` (ou au prochain cycle de cache 24 h).

## Configuration

| Variable | Défaut | Rôle |
|---|---|---|
| `UPDATES_ENABLED` | `true` | désactive complètement le vérificateur |
| `UPDATES_REPO` | `nik121212T/proxy-rotator` | dépôt interrogé |
| `UPDATES_CHECK_INTERVAL_HOURS` | `24` | TTL du cache |
| `UPDATES_TIMEOUT_S` | `5.0` | timeout HTTP vers api.github.com |

## Sécurité

- aucune signature cryptographique (bêta privée, ami de confiance)
- `git merge --ff-only` : impossible d'avaler un historique divergent
- `remote` vérifié contre `UPDATES_REPO` : impossible de pointer vers un fork
- la mise à jour exige une confirmation explicite dans l'UI, jamais silencieuse
- `UPDATES_ENABLED=false` dans `.env` coupe tout contact avec api.github.com

Pour une diffusion plus large, passer à **option B** (release tarball signé)
ou **option C** (PyInstaller + PyUpdater) — voir discussion d'audit.
