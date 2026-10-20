# Mises à jour par releases GitHub

Disponible pour les **clones Git sur la branche main**, sur macOS et Windows.
Une archive ZIP sans `.git` doit être mise à jour manuellement. Git et Python
restent nécessaires ; aucun Bash n’est nécessaire sous Windows.

## Dépôt privé : accès requis

Chaque utilisateur doit disposer d’un accès au dépôt privé. Le vérificateur
utilise `UPDATES_GITHUB_TOKEN` si renseigné dans `.env` (jeton autorisé à lire
les contenus du dépôt). Sinon, il utilise la connexion locale de GitHub CLI
(`gh auth login`). Le jeton n’est ni affiché ni transmis aux processus d’installation.

L’accès Git est distinct de l’accès API : configurez aussi Git pour pouvoir
récupérer le dépôt sans interaction, par exemple `gh auth setup-git` pour HTTPS
ou une clé SSH autorisée. Un jeton dans `.env` seul ne configure pas Git.

Un tag Git ne suffit pas : une **release stable publiée** doit exister.
L’erreur 404 peut signifier absence de release ou accès privé refusé.

## Utilisation

Ouvrez **Paramètres → Mises à jour → Vérifier maintenant**. Le dashboard et
les paramètres consultent également le vérificateur à leur ouverture ; le
résultat reste en cache 24 heures par défaut. Il n’y a pas de boucle périodique.

Le bouton *Mettre à jour* demande confirmation pour la version affichée :

1. Vérification de la release stable choisie, du dépôt `origin`, de la branche
   `main`, de l’absence de modifications locales et de sessions navigateur.
2. Récupération du **tag exact**, avec refus d’un historique divergent.
3. Création d’un environnement Python dans `.updates/<identifiant>/venv`,
   installation des dépendances, contrôle `pip check` et import de l’application.
4. Nouvelle vérification des modifications locales, puis avance du code vers
   le commit de la release et activation via `.updates/active.json`.
5. **Relance manuelle** avec le lanceur habituel. Le processus déjà démarré
   continue d’utiliser son ancien environnement jusqu’à sa fermeture.

L’environnement en cours n’est jamais modifié par pip. Si la préparation
échoue, son dossier est supprimé. Si l’activation échoue, le code et le pointeur
précédents sont restaurés. Le délai maximum est de cinq minutes ; les processus
d’installation sont arrêtés à expiration. Une seule installation est autorisée
à la fois. La préparation peut utiliser plusieurs centaines de Mo.

Les anciens environnements sont conservés, sans nettoyage automatique. Ne
supprimez pas `.venv` (utilisé par le lanceur), ni l’environnement référencé
par `.updates/active.json`. Les données, profils et `.env` restent dans le
dossier du projet et ne sont pas inclus dans les archives distribuées.

## Limites et récupération

Une coupure de courant entre l’avance Git et l’écriture du pointeur n’est pas
une transaction atomique : le lanceur refuse une incohérence code/environnement
plutôt que de lancer une combinaison incorrecte. Dans ce cas, faites vérifier
le commit et le pointeur avant de relancer. Ne supprimez pas vos données.

Les installations antérieures à v0.1.2 ne disposent pas de ce mécanisme fiable.
Pour cette première migration, fermez l’application, sauvegardez les données,
vérifiez que le clone est propre sur `main`, puis récupérez le tag v0.1.2 et
avancez jusqu’à ce tag en fast-forward. Relancez le lanceur habituel : il mettra
à niveau l’environnement initial. Le script historique `scripts/update.sh`
refuse désormais l’ancienne installation directe dans l’environnement actif.

## Configuration

| Variable | Défaut | Rôle |
|---|---|---|
| `UPDATES_ENABLED` | `true` | `false` désactive vérification et installation |
| `UPDATES_REPO` | `nik121212T/proxy-rotator` | Dépôt autorisé |
| `UPDATES_GITHUB_TOKEN` | absent | Accès API privé ; sinon connexion locale `gh` |
| `UPDATES_CHECK_INTERVAL_HOURS` | `24` | Durée du cache |
| `UPDATES_TIMEOUT_S` | `5.0` | Délai des requêtes de vérification |

Aucune télémétrie. Cette fonctionnalité autorise explicitement les requêtes
GitHub et le téléchargement des dépendances lors d’une installation demandée.
`UPDATES_ENABLED=false` désactive les contacts GitHub du vérificateur.
Les tags ne sont pas vérifiés par signature : la confiance repose sur le dépôt
configuré et ses mainteneurs. Ne déplacez pas un tag de release déjà publié.

## Publier une version

Synchroniser `app/__init__.py` et `pyproject.toml`, mettre à jour le changelog,
exécuter les tests et la CI macOS/Windows/Linux, puis publier une release GitHub
stable sur le commit validé. Joindre uniquement l’archive produite par
`scripts/build_windows.py`, qui utilise une liste explicite de fichiers publics.
Le paquet ZIP permet une installation manuelle ; utiliser un clone pour les
mises à jour intégrées.
