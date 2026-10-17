# Mesures de performance — 17 octobre 2026

Première passe : chargement des historiques, rendu des lignes et sélection dans
la passerelle. Mesures locales sur le Mac Apple Silicon de développement,
Python 3.11. Les chiffres sont indicatifs, pas une garantie de débit Internet.

## Reproduire

Depuis la racine du dépôt :

```sh
PYTHONPATH=. .venv/bin/python scripts/benchmark_performance.py
```

Le script crée une base SQLite temporaire par scénario, des identités fictives
et un faux upstream HTTP sur 127.0.0.1. Aucun proxy utilisateur, aucune base de
production et aucun accès Internet ne sont utilisés. Aucun moteur navigateur
n’est lancé. Les bases sont supprimées à la fin.

Pools de 100, 1 000 et 10 000 proxies disponibles, chacun avec un résultat de
vérification. Les rendus sont mesurés après chauffage des templates : médiane
de 3 pages complètes, 20 lignes isolées et 100 sélections pondérées. Le débit
correspond à 100 requêtes HTTP, concurrence 10, à travers la vraie passerelle
vers le faux upstream. Le pic mémoire est mesuré séparément avec tracemalloc
sur un rendu : allocations Python temporaires, pas la RAM totale du processus.

## Avant / après

Avant : code `f41d2f8` avec le même script. Après : changements T17.
Un passage par version ; les variations de charge système restent possibles.

| Pool | Chargement avant → après | Ligne avant → après | Sélection avant → après | Débit HTTP local avant → après |
|---|---|---|---|---|
| 100 | 13,84 → 3,49 ms | 1,11 → 0,09 ms | 0,35 → 0,16 ms | 790 → 964 req/s |
| 1 000 | 134,67 → 28,63 ms | 8,90 → 0,14 ms | 3,87 → 1,47 ms | 182 → 418 req/s |
| 10 000 | 1 367,40 → 340,49 ms | 95,66 → 0,67 ms | 43,91 → 16,82 ms | 16,3 → 38,7 req/s |

Corrections :

- Une requête SQL en flux pour les historiques, au lieu d’une requête par proxy.
  L’ordre des derniers tests, les 20 latences et la dernière IP observée restent
  identiques. Le lecteur conserve au plus 50 résultats par groupe.
- Une mise à jour de ligne ne prépare plus les statistiques, l’activité, le tri
  et les options géographiques de l’ensemble du dashboard. Les filtres et la
  suppression d’une ligne devenue invisible restent appliqués.
- La rotation calcule les scores courants sans copier tous les objets Proxy.
  Seul le proxy choisi est copié. Aucun cache de score : décroissance temporelle,
  seuils, filtres, priorités et quarantaines sont réévalués à chaque choix.

## Limites et prochaine priorité

Le rendu complet de 10 000 lignes reste autour de 962 ms (968 ms avant),
19 709 286 octets de HTML et 95 MiB de pic d’allocations Python temporaires.
Il n’y a pas de gain revendiqué sur le tableau complet ou la mémoire totale.
La prochaine priorité est une pagination côté serveur, avec conservation des
filtres, du tri et des mises à jour en direct. Elle doit être vérifiée séparément
pour éviter de perdre ou dupliquer des lignes pendant les changements d’état.

La sélection reste linéaire avec la taille du pool. Le benchmark ne couvre pas
la latence Internet, TLS/CONNECT, SOCKS, les téléchargements volumineux, le coût
d’affichage du DOM, les moteurs navigateur ni un historique rempli à 50 tests
par IP. Les tests fonctionnels existants couvrent les protocoles, mais leurs
débits ne sont pas extrapolés depuis cette mesure HTTP locale.

## Vérification sur le pool réel — 17 octobre 2026

Après l’import utilisateur : 12 proxies enregistrés, tous disponibles au début
des mesures. Lecture seule de l’application locale : médiane de 5 requêtes après
une requête de chauffage, sans requête de test ni mutation sur l’instance active.

| Mesure | Résultat |
|---|---|
| Dashboard HTTP local | 3,89 ms médiane ; 34 244 octets |
| Snapshot du tableau | 2,82 ms médiane ; 27 664 octets |
| Paramètres | 1,24 ms médiane |
| Ligne isolée | 1,02 ms médiane |
| Chargement du pool depuis une copie mémoire | 0,64 ms médiane |
| Sélection à tour de rôle | 0,024 ms médiane |
| Pic d’allocations d’un rendu Python | 0,169 MiB |

Pour les tests réseau, la base a été ouverte avec `mode=ro` et sauvegardée dans
une base SQLite exclusivement en mémoire. Les passerelles de test ont utilisé
cette copie sur des ports localhost éphémères ; leurs mises à jour de santé et
leur activité n’ont pas été écrites dans le pool utilisateur.

- HTTP : 3 requêtes HTTPS sur 3 réussies, médiane 1 933 ms, maximum 1 983 ms,
  3 IP de sortie distinctes.
- SOCKS5 : 3 requêtes HTTPS sur 3 réussies, médiane 2 138 ms, maximum 3 005 ms,
  3 IP de sortie distinctes.

Ce sont six petits contrôles vers la cible de vérification configurée, pas un
test de débit ni une certification de chacun des 12 proxies. Les IP et accès
ne sont pas consignés. Le coût local de sélection est faible face au délai
réseau mesuré ; cette observation ne sépare pas le fournisseur, Internet et
le service cible. La médiane HTTP locale du dashboard masque un maximum de
74,55 ms dans cette courte série ; ne pas interpréter ces valeurs comme des
percentiles de production. Les mesures ne vérifient pas les 113 autres lignes
du fichier initial et ne permettent pas de conclure à leur indisponibilité.
