# Installation Windows

Cible de validation : Windows 11 x64 avec une session utilisateur graphique.
Utilisez **Python 3.11 x64** avec « Add python.exe to PATH ». Le lanceur accepte
3.11 à 3.13 x64 ; la CI cible 3.11. Python 3.14, ARM64 natif et Python 32 bits
sont refusés par ce lanceur avec les dépendances actuellement épinglées.

## Première installation automatique

1. Extraire entièrement `proxy-rotator-windows.zip` dans un dossier personnel
   accessible en écriture, hors `Program Files` et hors dossier synchronisé.
2. Double-cliquer sur **Lancer.bat**, à la racine du dossier extrait.
3. Suivre les six étapes affichées. Le navigateur s’ouvre lorsque le serveur répond.
4. Garder la console ouverte et importer ses propres proxies.

Le lanceur recherche Python 3.11–3.13 x64. Si nécessaire, il télécharge
[Python 3.13.16 depuis python.org](https://www.python.org/downloads/release/python-31316/),
vérifie sa signature Windows et son éditeur Python Software Foundation, puis
l’installe pour l’utilisateur, sans modifier le PATH ni les associations de fichiers.
Les options d’installation suivent la [documentation officielle](https://docs.python.org/3.13/using/windows.html#installing-without-ui).

Il crée automatiquement `.env` à partir de `.env.example`, avec une clé aléatoire
propre à cette installation. Une configuration déjà renseignée est conservée.
Si une base existe mais que la clé manque, il s’arrête : il faut restaurer
le `.env` original, pas créer une nouvelle clé qui ne pourrait pas lire les données.

À chaque lancement, il vérifie l’environnement, les versions des dépendances et
leurs options, puis l’import de l’application. Les dépendances absentes ou
incompatibles sont réinstallées, avec les détails affichés ligne par ligne.
Un environnement incompatible est sauvegardé avant recréation. Deux lancements
simultanés du même dossier sont refusés. Un port déjà occupé est signalé.

L’adresse par défaut est **http://127.0.0.1:8080**. Si le navigateur ne s’ouvre
pas automatiquement, copier cette adresse dans sa barre d’adresse. Les moteurs
Chromium/Firefox/WebKit restent des téléchargements à la demande dans l’application.
`Lancer.bat` conserve la fenêtre ouverte en cas d’erreur. PowerShell est utilisé
uniquement pour ce lancement ; aucune politique système permanente n’est modifiée.
Une politique d’entreprise, un antivirus, des droits insuffisants, un manque de
place ou l’absence d’Internet peuvent empêcher l’installation : le lanceur affiche
l’étape en échec plutôt que de contourner ces contrôles.

Pour une préparation sans démarrage :
`powershell -NoProfile -ExecutionPolicy Bypass -File scripts\bootstrap_windows.ps1 -PrepareOnly`.
Le lanceur historique `scripts\start.bat` reste disponible pour les installations
préparées manuellement ; l’entrée recommandée est désormais `Lancer.bat`.

Une connexion Internet est nécessaire pour les installations. Le navigateur
utilise une passerelle locale ; ce n’est pas un VPN pour tout Windows.
La page de départ est Whoer. La rotation s’applique aux nouvelles connexions ;
les connexions déjà ouvertes peuvent conserver leur proxy.

## En cas de problème

- **Python incompatible** : installer Python 3.11 x64 et recréer `.venv` après
  avoir arrêté l’application. Ne pas copier un environnement virtuel macOS.
- **Téléchargement échoué** : vérifier Internet, l’espace disque, les droits du
  dossier et l’accès au CDN Playwright, puis réessayer depuis Navigateurs.
- **Moteur téléchargé mais ouverture impossible** : vérifier l’accès au bureau
  et les composants Visual C++ et Media Foundation, notamment sur les éditions
  Windows N/Server. « Téléchargé » n’est pas une validation du lancement.
- **Fermeture incomplète** : fermer la fenêtre puis utiliser **Réessayer la
  fermeture**. Le profil reste visible dans la liste tant que le nettoyage
  échoue ; sa passerelle est arrêtée. À l’arrêt de l’application, les échecs
  sont journalisés. Après redémarrage, les anciennes sessions ne sont pas
  restaurées : un profil résiduel peut rester dans `data/browser-profiles`.
- **Aucun proxy pour la rotation** : vérifier les filtres de pays, ville,
  région et priorité. Un proxy actif peut être exclu par ces filtres.

## Distribution et vérification

`python scripts/build_windows.py` crée `dist/proxy-rotator-windows.zip` avec
une liste explicite de fichiers applicatifs. `.env`, bases, sels, profils,
caches, environnements virtuels et historique Git sont exclus. Le lanceur crée le secret du destinataire ; celui-ci importe ses propres proxys.

Le job CI **Windows browser engines** installe Chromium, Firefox et WebKit et
exécute les scénarios réels HTTP/HTTPS, avec/sans authentification, proxy fixe,
rotation, pool vide, absence de repli HTTP direct, fermeture, libération du port
et suppression du profil. Les serveurs de test sont locaux ; seuls ces tests
acceptent leur certificat TLS temporaire. Le produit conserve ses vérifications TLS.

Le 16 octobre 2026, la [CI du commit 58f7cd6](https://github.com/nik121212T/proxy-rotator/actions/runs/38912519830)
a réussi sur Windows, macOS et Linux, ainsi que l’audit des dépendances.
Les six scénarios des navigateurs réels ont réussi sur le runner Windows
(Python 3.11), et Ruff/mypy sont verts. Les trois moteurs ont aussi été testés
réellement sur macOS. Un essai manuel sur Windows 11 reste recommandé,
notamment pour les navigateurs installés, les chemins accentués, l’arrêt du
terminal et les fuites DNS/WebRTC. Les tests HTTP ne sont pas une certification
d’absence de toute fuite réseau.
