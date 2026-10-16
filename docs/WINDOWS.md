# Installation Windows

Cible de validation : Windows 11 x64 avec une session utilisateur graphique.
Utilisez **Python 3.11 x64** avec « Add python.exe to PATH ». Le lanceur accepte
3.11 à 3.13 x64 ; la CI cible 3.11. Python 3.14, ARM64 natif et Python 32 bits
sont refusés par ce lanceur avec les dépendances actuellement épinglées.

## Première installation

1. Extraire entièrement `proxy-rotator-windows.zip` dans un dossier personnel
   accessible en écriture, hors `Program Files` et hors dossier synchronisé.
2. Copier `.env.example` en `.env`. Remplacer `MASTER_SECRET=CHANGE_ME_NOW`
   par une longue phrase secrète propre à cette installation et la conserver.
3. Ouvrir un terminal dans ce dossier et lancer `scripts\start.bat`.
   La première exécution crée `.venv` et télécharge les dépendances.
4. Ouvrir <http://127.0.0.1:8080>, importer les proxys puis les vérifier.
5. Dans **Navigateurs**, choisir un navigateur installé ou télécharger un moteur
   dédié. Les fenêtres s’ouvrent sur le PC qui exécute le serveur.

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
caches, environnements virtuels et historique Git sont exclus. Le destinataire
crée son propre secret et importe ses propres proxys.

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
