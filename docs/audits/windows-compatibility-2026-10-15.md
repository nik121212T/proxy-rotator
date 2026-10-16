# Audit Windows — 15 octobre 2026

## Verdict

Le projet a une base Windows fonctionnelle, mais la version actuelle ne peut pas être déclarée entièrement validée sur Windows. Cible initiale raisonnable : Windows 11 x64, Python x64, session utilisateur avec bureau. Windows Server 2019+ est également dans le périmètre déclaré par Playwright, sous réserve des composants système et d’un bureau utilisable. Windows 10 n’est plus dans le périmètre officiellement annoncé pour les moteurs dédiés actuels ([Playwright](https://playwright.dev/python/docs/intro)). Cela ne démontre pas que le serveur Python seul ne peut pas y fonctionner.

Périmètre : HEAD local `b0ac87b`, avec changement non commité de la page de départ vers Whoer. Aucune correction de fonctionnement, installation de paquet, modification de données utilisateur ou publication réalisée pour cet audit.

## Méthode et force des preuves

- Inspection des versions exactes de requirements.txt, code applicatif, lanceur Windows, tests et workflow CI.
- Recherche dans les documentations officielles Python, Microsoft, Playwright, Cryptography et les publications PyPI.
- Résolution des dépendances en simulation : Windows x64 / CPython 3.11, uniquement paquets binaires ou universels, environnement vierge. Les 79 paquets directs/transitifs sont résolus sans conflit. Aucun paquet installé.
- Important : `pip --platform` seul, exécuté sur macOS, conserve certains marqueurs de l’hôte et demande à tort uvloop. Une deuxième simulation a explicitement évalué les marqueurs `os_name=nt`, `sys_platform=win32`, `platform_system=Windows`, `platform_machine=AMD64` et Python 3.11. Le résultat de cette seconde simulation est retenu. Ce n’est pas une exécution Windows.
- Vérification des distributions PyPI pour les 22 dépendances directes épinglées et pydantic-core 2.23.4, pour six combinaisons Python/architecture.
- Consultation des résultats réels GitHub Actions existants, sans déclencher ni publier une nouvelle exécution.

## Preuve Windows réelle déjà disponible

[Exécution Windows du 14 octobre 2026](https://github.com/nik121212T/proxy-rotator/actions/runs/38623401294/job/116056377140), commit `18bceae7972da50bba8bfe4354b57a98a28ae0e5`, sur Windows Server 2025, Python 3.11 :

- Installation des dépendances : réussie.
- Tests : **379 réussis, 2 échoués, 9 ignorés, 11 sous-tests réussis**.
- Ruff et mypy : non exécutés dans ce job, car les tests ont échoué.
- Cette version précède les navigateurs intégrés : elle ne valide pas leur lancement Windows.
- Le workflow actuel possède bien une matrice Windows/macOS/Linux, mais n’installe pas les trois moteurs et n’effectue pas de navigation réelle dans ces trois moteurs.

Les deux échecs :

1. `test_connection_failure_reason_reaches_the_pool` : reçoit `upstream_error` au lieu de `Connection failure`. Le détecteur actuel compare seulement certains errno POSIX. Une simulation locale d’un OSError portant le code Winsock 10061 ne le reconnaît pas, contrairement au code POSIX ECONNREFUSED. C’est une explication cohérente avec la CI, sans trace brute de l’exception historique permettant de confirmer son numéro exact. L’échec reste enregistré dans le pool et la réponse reste 502 ; ce n’est pas une preuve de connexion directe. Voir app/burn_detector.py et app/gateway_http.py. [Codes Winsock officiels](https://learn.microsoft.com/en-us/windows/win32/winsock/windows-sockets-error-codes-2).
2. `test_timeout_is_safe_and_requests_for_same_proxy_coalesce` : impose 20 ms de délai et attend que le faux serveur ait déjà reçu une requête ; il en a reçu zéro. L’erreur Timeout attendue est bien obtenue. Hypothèse forte : test temporel fragile, à rendre synchronisé plutôt que conclure à une panne du produit. Windows a des différences de résolution d’horloge documentées. [Python asyncio Windows](https://docs.python.org/3.11/library/asyncio-platforms.html).

Ces deux tests et les conditions pertinentes restent présents dans le code actuel.

## Couverture fonctionnelle

| Élément | Conclusion de l’audit |
|---|---|
| Lanceur | `scripts\\start.bat` existe ; pas de start.bat à la racine. Utilise cmd.exe, Python >=3.11, crée .venv et installe requirements.txt. |
| Préparation | Nécessite Python accessible par `python`, fichier .env et MASTER_SECRET configuré. Pas d’installation automatique de Python, pas de .exe autonome. |
| Tableau de bord / compteurs / filtres / exports | Code serveur et assets web sans dépendance macOS identifiée ; validation finale dans Edge/Chrome Windows nécessaire. |
| Import avec/sans identifiants | Python portable ; les règles métier sont les mêmes. Import uniquement après test réussi conservé. |
| HTTP, CONNECT, SOCKS5 | Implémentation asyncio/TCP compatible en principe ; la CI historique couvre une grande partie des passerelles. Diagnostic Winsock à corriger. |
| Rotation, quarantaine, santé | Logique portable ; différences de temporisation à contrôler. Les performances observées sur Mac ne garantissent pas celles d’un PC. |
| SQLite / aiosqlite | Accès local et pathlib ; pas de dépendance POSIX obligatoire détectée. Le dossier du projet doit être accessible en écriture. |
| Chiffrement | cryptography 50.0.2 dispose d’un paquet Windows x64 utilisable pour Python 3.11. Pas besoin de compiler dans cette configuration. [Installation officielle](https://cryptography.io/en/latest/installation/). |
| Permissions des fichiers | La restriction chmod de la base est volontairement POSIX uniquement ; Windows dépend des ACL héritées. .env contient le secret maître. Installer dans un dossier personnel, pas partagé. |
| Géolocalisation / fournisseur externe | Clients HTTP Python portables ; disponibilité API, accès réseau et certificats restent dépendants de l’environnement. |
| Chrome / Edge / Brave / Chromium / Firefox installés | Chemins Windows prévus, profils séparés et arguments sans mot de passe proxy. Lancement réel Windows non vérifié pour cette version. |
| Trois moteurs dédiés | Distributions Windows proposées par Playwright ; téléchargements séparés. Firefox dédié est une version adaptée à Playwright ; WebKit n’est pas Safari. [Navigateurs Playwright](https://playwright.dev/python/docs/browsers). |
| Téléchargement des moteurs | Accès Internet requis ; des proxys d’entreprise/autorités de certificat peuvent nécessiter les variables Playwright prévues. L’app supprime actuellement stdout/stderr et ne montre qu’une erreur générique. |
| Arrêt des navigateurs | Branche Windows avec `taskkill /PID … /T /F`, adaptée à l’arrêt de l’arbre de processus. Il reste à vérifier disparition des enfants et libération des fichiers. [Microsoft taskkill](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/taskkill). |
| Cookies / profils temporaires | Suppression tentée à la fermeture mais erreurs ignorées : fichiers verrouillés ou en lecture seule peuvent laisser un profil. Point réel à corriger, pas seulement Windows. [Python shutil](https://docs.python.org/3.11/library/shutil.html). |
| Page Whoer | URL HTTPS normale, pas de dépendance OS spécifique. Le succès dépend du proxy et du site. Ce contrôle n’atteste pas à lui seul l’absence de toute fuite DNS/WebRTC. |
| Distribution à un autre utilisateur | Fournir le code sans .venv Mac, sans .env secret ni base personnelle. Recréer l’environnement et télécharger les moteurs sur Windows. Les environnements virtuels ne sont pas portables. [Python venv](https://docs.python.org/3.11/library/venv.html). |

## Python, architecture et paquets exacts

Disponibilité de wheels au moment de la consultation, et non certification d’exécution :

| Cible | Résultat |
|---|---|
| CPython 3.11 x64 | 23 paquets examinés disponibles ; résolution complète de 79 dépendances réussie. |
| CPython 3.12 x64 | Wheels disponibles pour les 23 paquets examinés ; résolution complète et exécution non faites. |
| CPython 3.13 x64 | Même constat limité aux 23 paquets. |
| CPython 3.14 x64 | Pas de wheel compatible pour pydantic-core 2.23.4. « Python 3.11+ » ne garantit donc pas une installation simple avec 3.14. Compiler depuis les sources serait une autre procédure, non validée. |
| CPython 3.11 ARM64 natif | Pas de wheels compatibles pour cryptography 50.0.2 et pydantic-core 2.23.4 dans cette matrice. Ne pas annoncer une prise en charge ARM64 native complète. |
| CPython 3.11 32 bits | Pas de wheel compatible pour cryptography 50.0.2. Ne pas recommander cette cible. |

Paquets inspectés : FastAPI 0.143.0, Starlette 1.7.0, AnyIO 4.15.1, Uvicorn 0.32.0, Pydantic 2.9.2, pydantic-settings 2.5.2, Jinja2 3.1.6, aiosqlite 0.20.0, aiohttp 3.14.5, aiohttp-socks 0.9.1, python-socks 2.8.2, cryptography 50.0.2, cachetools 5.5.0, click 8.1.7, Playwright 1.63.0, httpx2 2.13.1, pytest 9.1.1, pytest-asyncio 1.4.0, Hypothesis 6.169.0, Ruff 0.6.9, mypy 1.11.2, pip-audit 2.10.1, plus pydantic-core 2.23.4.

Sources exactes : API officielle PyPI `/pypi/<nom>/<version>/json`, fichiers listés par publication. Exemples : [Playwright 1.63.0](https://pypi.org/project/playwright/1.63.0/#files), [pydantic-core 2.23.4](https://pypi.org/project/pydantic-core/2.23.4/#files), [cryptography 50.0.2](https://pypi.org/project/cryptography/50.0.2/#files). Les dépendances transitives ne sont pas toutes épinglées : une installation ultérieure peut résoudre d’autres versions.

## Points techniques Windows à ne pas manquer

- **Boucle asyncio** : le lancement normal est monoprocès, sans reload. Uvicorn 0.32.0 installé ne force Selector sous Windows que si `use_subprocess` est vrai. Le mode normal conserve donc Proactor, nécessaire aux sous-processus Playwright. Ne pas introduire reload/multi-workers sans tests : Selector ne gère pas les sous-processus Windows. [Python](https://docs.python.org/3.11/library/asyncio-platforms.html). Observation de version locale, pas extrapolation de la dernière documentation Uvicorn.
- **Détection des navigateurs** : seulement emplacements connus sous PROGRAMFILES, PROGRAMFILES(X86), LOCALAPPDATA. Pas de recherche registre App Paths, d’exécutable choisi manuellement, ni de prise en charge explicite des variantes portables. « Aucun navigateur détecté » ne signifie pas « aucun installé ». [App Paths Microsoft](https://learn.microsoft.com/en-us/windows/win32/shell/app-registration).
- **Composants système** : le code Playwright 1.63.0 inspecté valide des DLL du runtime Visual C++ et de Media Foundation. Certaines éditions N/Server peuvent nécessiter ces composants. La présence de l’exécutable seule ne suffit pas : le catalogue de l’app utilise pourtant seulement `is_file()` pour annoncer « Prêt ».
- **Dossiers** : la base, le sel et les profils restent relatifs au projet ; le lanceur corrige le répertoire courant. Éviter Program Files et un dossier synchronisé/partagé pour cette version. Un transfert de base existante doit préserver son sel et le secret correspondant ; un nouvel utilisateur doit commencer avec ses propres données.
- **Élévation** : le serveur écoute en local et ne demande pas en soi les droits administrateur ; des installations de composants système peuvent les exiger. Aucune raison de désactiver globalement pare-feu ou antivirus.
- **Interface graphique** : les fenêtres s’ouvrent sur l’ordinateur exécutant le backend. Ce n’est pas un navigateur distant diffusé dans le dashboard, ni un service Windows sans bureau.
- **Proxy** : proxy de navigation web, pas VPN système. Les options Chromium/Firefox réduisent les chemins WebRTC directs ; WebKit n’a pas le même réglage dans ce code. Les tests de fuite doivent être faits moteur par moteur sous Windows.
- **Tests dev installés en production** : requirements.txt inclut aussi mypy, Ruff, pytest, pip-audit. Cela alourdit le démarrage et la matrice de compatibilité ; séparer runtime/dev serait utile.

## Corrections / validation à prioriser

1. Corriger la classification Winsock et rendre le test à 20 ms déterministe ; obtenir un job Windows vert sur le commit actuel.
2. Fixer une plage Python et architecture effectivement validée dans le lanceur et la documentation ; ne pas accepter indistinctement toute version >=3.11.
3. Ajouter des tests Windows réels des trois moteurs : installation, HTTP/HTTPS via proxy avec/sans identifiants, rotation, proxy fixe, zéro proxy sain, refus de repli direct, fermeture, ports libérés, profils supprimés.
4. Contrôler les erreurs de taskkill et de suppression de profil ; afficher un diagnostic exploitable sans exposer les identifiants.
5. Améliorer la détection des navigateurs (registre/chemin choisi) et le diagnostic des composants Windows manquants.
6. Reprendre les trois constats de la revue précédente, communs aux OS : réglages de passerelle non transmis aux sessions, nettoyage silencieux, état disponible ignorant les filtres de rotation.
7. Essai manuel Windows 11 dans une session standard : chemin avec espaces/accents, installation propre, double lancement, fermeture du terminal, import/export pays/compteurs, navigation Whoer et contrôle des fuites. Les résultats macOS et la CI historique ne remplacent pas cet essai.

Artefacts de travail : `/tmp/proxy-windows-3.11-win_amd64.json` (résolution complète), `/tmp/proxy-windows-wheels.json` (matrice des fichiers PyPI), `/tmp/proxy-windows-ci.log` (journal historique), scripts de simulation `/tmp/proxy_windows_resolve.py` et `/tmp/proxy_windows_wheels.py`. Ce sont des artefacts temporaires ; les conclusions et sources durables sont consignées ici.
