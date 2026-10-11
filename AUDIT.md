# Audit complet — Proxy Rotator Manager

**Périmètre** : dépôt `proxy-rotator` (v1 MVP), `app/` (4 313 lignes), `tests/` (4 377 lignes, 34 modules), `docs/`, `scripts/`, CI, configuration réelle (`.env`), base réelle (`data/proxies.db`).
**Méthode** : lecture directe du code, exécution de la suite de tests, lint/typage, audit de dépendances, inspection en lecture seule de la base de données de production, et reproduction instrumentée d'un bug de bout en bout. Trois analyses parallèles indépendantes (sécurité, data-plane, tests/documentation) ont été croisées et leurs constats revérifiés quand ils étaient à fort impact.

## Statuts utilisés

| Statut | Signification |
|---|---|
| **V** | Vérifié moi-même, par exécution ou lecture directe du code. |
| **A** | Constat d'analyse de code (sous-agent), cohérent avec le code lu, non rejoué par moi. |
| **R** | Réfuté — constat d'audit infirmé par vérification. |

Un constat **A** reste actionnable : il est étayé par des références précises, mais à confirmer lors du correctif.

## Statut des correctifs

Le **Palier 1** du plan (§12) a été implémenté dans le commit `34db8d2` :
**A1, A2, A3, A4, A8, A9, B1, B2, E1, E2** sont corrigés, chacun accompagné d'un
test de non-régression **vérifié en mutation** — chaque nouveau test a été
exécuté contre le code d'origine (un fichier restauré depuis `HEAD` à la fois) et
échoue ou se bloque, ce qui prouve qu'il détecte bien le défaut qu'il protège.
Un premier jet du test sur la taille de chunk négative passait contre l'ancien
code pour une mauvaise raison (un artefact du faux upstream) : il a été réécrit
avec un upstream muet.

Les constats ci-dessous sont conservés avec leur statut d'origine, pour garder la
trace de l'audit.

**Palier 2** (même journée) : **H1 et C1** sont corrigés, plus la partie
documentaire de **C4**.

- **H1** — `pip-audit -r requirements.txt` passe de **49 avis dans 5 paquets** à
  **aucune vulnérabilité connue** (fastapi 0.143.0, starlette 1.7.0 explicite,
  aiohttp 3.14.5, cryptography 50.0.2, anyio 4.15.1, pytest 9.1.1 +
  pytest-asyncio 1.4.0 + hypothesis 6.169.0, `httpx` de test remplacé par
  `httpx2`). La montée de version a révélé et fait corriger trois vrais
  problèmes : `aiohttp.BasicAuth`/`proxy_auth` dépréciés en 3.14 (les
  identifiants passent désormais par le userinfo de l'URL du proxy), la route
  WebSocket qui laissait échapper un `CancelledError` pendant son nettoyage, et
  les faux upstreams de test qui ne possédaient pas leurs tâches. Un job CI
  bloquant `audit` exécute maintenant réellement `pip-audit`.
- **C1** — trois garde-fous ASGI (`HostGuard`, `CrossSiteGuard`, `TokenGuard`)
  plus un refus de démarrage pour tout bind hors boucle locale sans
  `ALLOW_REMOTE_BIND` + `ADMIN_TOKEN` + `ADMIN_ALLOWED_HOSTS`. Vérifié sur une
  vraie instance uvicorn en plus des tests ASGI.
- **C4** (partiel) — `docs/SECURITY.md` ne revendique plus de contrôles
  inexistants (lint rule, `app.log.mask()`, `tests/security/`, `pip-audit` CI) et
  liste les risques résiduels.

**Complément d'entrée** (même journée) : **A5, A6, A10 et A11** sont corrigés,
chacun avec un test vérifié en mutation.

- **A5** — `Expect: 100-continue` est traité par le gateway (RFC 9110 §10.1.1) et
  l'en-tête n'est plus transmis ; plus d'interblocage client/serveur.
- **A6** — budget d'inactivité sur la lecture du corps client, classé en erreur
  *client* (400) et non en échec du proxy.
- **A10** — les identifiants SOCKS5 amont ne passent plus par une URL `socks5://`
  (dont le `unquote` transformait `p%41ss` en `pAss` et faisait lever un
  `ValueError` sur `/ ? # [`) : ils sont passés en arguments nommés à
  `python_socks` depuis `app/socks_upstream.py`. L'usage déprécié de
  `aiohttp_socks.open_connection` disparaît au passage.
- **A11** — `LimitOverrunError` sur une ligne d'en-tête trop longue → 400 au lieu
  de 500.

**F1 reste ouvert et hors de portée** : aucun proxy vivant dans la base
(0 `alive`, dernière réussite le 2026-10-09T08:27), donc aucune preuve de rotation
ne peut être produite.

**Restent ouverts** : A7, A12–A17, B3–B8, C2, C3, C5, C6, E3–E8, F1–F9,
G1–G5, H3, H4.

## Résultat des vérifications globales

```
pytest -q          → 281 passed, 1 skipped, 1 failed (63 s)   [avant correctifs]
                      291 passed, 1 skipped, 1 failed (59 s)   [après correctifs]
ruff check app tests → All checks passed
mypy app           → Success: no issues found in 34 source files
pip check          → No broken requirements found
pip-audit -r requirements.txt → 49 vulnérabilités connues dans 5 paquets
git remote -v      → (vide : aucun dépôt distant, CI jamais exécutée)
git rev-list --count HEAD → 53 commits
```

Le seul test rouge, `tests/test_start_command.py::test_finder_restarts_only_same_project`, échoue parce que `/bin/ps` est bloqué par le sandbox de l'agent (`ps: Operation not permitted`). Le launcher ne peut donc pas énumérer les processus. Ce n'est **probablement pas un bug du code** ; à revérifier dans un terminal normal.

---

# 1. Verdict

Projet nettement au-dessus de la moyenne de ce qu'un agent produit : architecture en couches réelle, sécurité des identifiants **effective et vérifiée en base de données**, suite de tests d'intégration authentique (un seul mock dans tout le dépôt), discipline de commits et documentation honnête sur ses propres limites.

Les faiblesses sont localisées et nommables : **la couche data-plane des gateways** (le cœur de la valeur produit) contient plusieurs bugs de correction réels, dont un que j'ai reproduit — une seconde réponse HTTP écrite dans le même socket client. À cela s'ajoutent un cycle de vie de santé qui laisse des proxies bloqués à vie, des dépendances figées porteuses de 49 vulnérabilités connues, deux tests annoncés comme preuve du retry qui sont vides, et une documentation qui décrit des garde-fous inexistants.

> **Mise à jour (Palier 1 livré, commit `34db8d2`)** : les huit défauts de
> correction du data-plane et du cycle de vie de santé listés au §2, ainsi que
> les deux faux tests, sont corrigés et couverts par des tests de non-régression
> vérifiés en mutation. Le verdict ci-dessus décrit l'état **avant** ces
> correctifs ; voir « Statut des correctifs ». Restent ouverts la sécurité
> (C1–C6), les dépendances (H1), la robustesse d'entrée restante (A5–A7) et la
> mise à jour documentaire (F1–F9).

Le produit n'est **pas** cassé dans son concept : le chiffrement, le fail-closed, la protection DNS et la concurrence du pool sont solides. Il n'est pas encore fiable comme proxy du quotidien, et la preuve d'intégration exigée par le cahier des charges (« trois IP de sortie différentes ») n'a jamais été obtenue.

**Note globale indicative : 6,5/10** — 8,5/10 sur la structure, la sécurité des secrets et les tests ; 4/10 sur la robustesse du data-plane et la justesse de la documentation.

---

# 2. Résumé exécutif — les 10 constats à traiter

| # | Sévérité | Constat | Statut | Fichier |
|---|---|---|---|---|
| A1 | **Élevée** | Retry après écriture partielle : une 2ᵉ réponse HTTP est concaténée dans le même socket client | V (reproduit) · ✅ corrigé | `app/gateway_http.py:441-464` |
| A2 | **Élevée** | Timeout de 15 s appliqué au transfert complet : gros téléchargements corrompus, proxy sain pénalisé | V · ✅ corrigé | `app/gateway_http.py:441` |
| B1 | **Élevée** | Un proxy qui rate son 1ᵉʳ check reste `unknown` **à vie** : jamais retesté, jamais actif | V (code + 74/100 en base) · ✅ corrigé | `app/pool.py:344`, `app/health_checker.py:271` |
| H1 | **Élevée** | 49 vulnérabilités connues dans 5 dépendances figées ; `pip-audit` absent de la CI | V · ✅ corrigé | `requirements.txt` |
| A3 | **Élevée** | Désynchronisation CL.TE : `Content-Length` client transmis alors que le corps est décodé en chunked | V · ✅ corrigé | `app/gateway_http.py:37-49`, `:687`, `:541` |
| A8 | **Élevée** | POST/PUT ≤ 1 Mo rejoués après un 429/503 de l'origine → double effet de bord | V · ✅ corrigé | `app/gateway_http.py:430-459` |
| E1 | **Élevée** | Les 2 tests cités comme preuve du retry ne testent rien (passent sans la fonctionnalité) | V · ✅ corrigé | `tests/test_gateway_http.py:525,549` |
| C1 | **Élevée si LAN** | Aucune authentification ; `/api/proxies/export` renvoie tous les mots de passe en clair | V · ✅ corrigé (export toujours en clair sur loopback, documenté) | `app/routes/proxies.py:88` |
| A4 | Moyenne-élevée | Taille de chunk hexadécimale signée acceptée → `read(-1)` → mémoire non bornée + blocage | V · ✅ corrigé | `app/gateway_http.py:741` |
| B2 | Moyenne | Les tunnels CONNECT enregistrent une latence de 0 → médiane écrasée, score gonflé à 100 | V · ✅ corrigé | `app/gateway_http.py:335` |

---

# 3. Data-plane : les gateways HTTP et SOCKS5

Le cœur du produit. C'est là que se concentrent les bugs de correction.

## A1 — Retry après écriture partielle : seconde réponse HTTP dans le même socket · **Élevée** · V (reproduit)

`app/gateway_http.py:441-464`. La boucle de retry enveloppe l'essai complet dans `asyncio.wait_for(..., timeout=15)`, **y compris la boucle d'écriture vers le client** (`:599-611`). Si le budget expire ou si l'upstream coupe **après** l'envoi du status line, le `except` fait `mark_fail` puis `continue` : `_forward_attempt` est rappelé et écrit une **seconde réponse HTTP complète** dans le même socket.

Aucun indicateur « des octets ont déjà été envoyés » n'existe dans le fichier, ce qui contredit la docstring `:510-513` (« returns a burn reason *before* any bytes left the gateway »). Le même schéma existe dans `_single_forward` (`:479-497`), où un 502 est ajouté après une réponse partielle.

**Reproduction** (upstream annonçant 1 Mo et n'envoyant que 100 octets, puis fermant) :

```
STATUS_LINES_SEEN = 2
RAW_START = HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nVia: 1.1 proxy-rotator\r\nContent-Length: 1000000\r\n\r\nAAAA…
RAW_END   = …HTTP/1.1 502 Bad Gateway: Response payload is not completed…\n
```

**Impact réel** : tout téléchargement dépassant `GATEWAY_TIMEOUT_S` (15 s par défaut) produit un fichier silencieusement corrompu, et le proxy — pourtant sain — est marqué en échec puis dégradé. C'est exactement le scénario d'usage « navigateur derrière le gateway ».

**Correctif** : un booléen `response_started` positionné avant le premier `writer.write` de la réponse, et un `return` inconditionnel dès qu'il est vrai. **Effort : S.**

## A2 — Timeout global appliqué au transfert complet · **Élevée** · V

Même origine que A1. `aiohttp.ClientTimeout(total=None, sock_connect=self._timeout_s)` (`:539`) n'impose aucune limite au corps de la réponse ; c'est le `wait_for` externe qui coupe tout à 15 s. Conséquences : les gros transferts légitimes échouent, et la branche `except` déclenche le retry défectueux de A1.

**Correctif** : séparer les budgets — connexion (`sock_connect`), en-têtes, et **inactivité** du corps (`sock_read`), au lieu d'une échéance totale sur la durée du transfert. **Effort : S.**

## A3 — Désynchronisation CL.TE (request smuggling) · **Élevée** · V

`_HOP_BY_HOP` (`:37-49`) retire `transfer-encoding` mais **pas** `content-length`. Or `_collect_body_if_small` retourne `None` dès que `transfer-encoding: chunked` est présent (`:687-688`), `_has_body` retourne `True` (`:660-661`), et le corps est donc décodé par `_chunked_body` — pendant que l'en-tête `Content-Length` du client est transmis tel quel à `session.request(headers=..., data=body)` (`:541-549`).

Vérifié dans la source d'aiohttp 3.10.11 installée (`client_reqrep.py:591-599`) : le passage en chunked n'a lieu que si `CONTENT_LENGTH` est **absent** des en-têtes. Ici il est présent → aiohttp conserve la valeur du client et écrit le corps **décodé** en clair, sans cadrage ni validation.

**Impact** : une `Content-Length: 0` + `Transfer-Encoding: chunked` + corps permet à un processus local d'écrire dans le flux ce que le proxy amont interprétera comme le début de la requête suivante. Vecteur localhost uniquement, mais c'est précisément le type de bug qu'un projet de proxy ne peut pas se permettre. Les `Content-Length` dupliqués/contradictoires ne sont ni normalisés ni rejetés (la dernière valeur gagne, `:681-691`).

**Correctif** : rejeter `400` toute requête portant à la fois CL et TE, ou plusieurs CL divergents. **Effort : S.**

## A4 — Taille de chunk signée → mémoire non bornée · **Moyenne-élevée** · V

`app/gateway_http.py:741` : `size = int(size_text, 16)`. Python accepte le signe — vérifié : `int(b"-1", 16) == -1`. On arrive ensuite à `remaining = -1` puis `reader.read(min(-1, _TUNNEL_CHUNK))`. Or `StreamReader.read(n < 0)` lit **jusqu'à EOF** (vérifié dans la source de `asyncio`), en accumulant tous les octets en RAM.

**Impact** : un `POST` avec `Transfer-Encoding: chunked` et une ligne `-1\r\n` suivie d'un flux infini fait grossir la mémoire du processus sans limite et bloque la connexion indéfiniment. Les corps chunked empruntent toujours le chemin non bufferisé (`_single_forward`), donc c'est atteignable.

**Correctif** : exiger `^[0-9a-fA-F]+$` et plafonner la taille d'un chunk. **Effort : S.**

## A5 — `Expect: 100-continue` transmis mais jamais honoré · **Moyenne** · V

Le mot `Expect` n'apparaît **nulle part** dans `app/` (grep exhaustif) : il n'est pas dans `_HOP_BY_HOP`, donc il est transmis à l'origine, et le gateway n'émet jamais `HTTP/1.1 100 Continue`. Un client conforme attend l'interim response, le gateway attend le corps : interblocage jusqu'au timeout du client (curl ne s'en sort que grâce à son repli à 1 s).

**Correctif** : répondre `100 Continue` soi-même quand `Expect: 100-continue` est présent. **Effort : S.**

## A6 — Aucun timeout sur la lecture du corps client · **Moyenne** · V

`asyncio.timeout(self._timeout_s)` ne couvre que la ligne de requête et les en-têtes (`:241-243`). La lecture du corps (`:422` → `:696` `readexactly`, générateurs `:726-733`) n'a aucune limite de temps. Un client annonçant 1 Mio puis n'envoyant qu'un octet par heure immobilise une tâche et un tampon indéfiniment (slow-loris post-en-têtes).

**Correctif** : timeout d'inactivité autour de la lecture du corps. **Effort : S.**

## A7 — Aucun plafond de connexions ni mutualisation · **Moyenne** · V

Les deux gateways utilisent `asyncio.start_server` nu (`gateway_http.py:196`, `gateway_socks.py:85`) ; un grep de `Semaphore|Lock|MAX_CONN` dans ces fichiers ne retourne **rien**. Chaque requête construit une `aiohttp.ClientSession` neuve (`:534`) : aucune réutilisation de connexion, contrairement à ce que décrit `ARCHITECTURE.md:72` (« per-conn asyncio.Lock on upstream », « aiohttp.ClientSession pooling »).

**Impact** : N clients locaux ⇒ N tâches, ~2N sockets vers les proxies, N sessions ; plus ≤ 1 Mio tamponné par requête (`:644`), sans contrôle d'admission, aggravé par A6 et l'absence de timeout sur les tunnels inactifs. Seul `HealthChecker` a un sémaphore (`health_checker.py:45`).

**Correctif** : plafond d'admission par gateway + session par proxy (ce qui, pour un rotateur, est cohérent : la rotation se fait par connexion amont). **Effort : M.**

## A8 — Requêtes non idempotentes rejouées · **Élevée** · V

`app/gateway_http.py:421-459` : tout corps ≤ 1 Mio est bufferisé puis rejoué via `_bytes_async_iter` sur un proxy différent, jusqu'à `max_retry + 1` essais. Le déclencheur est le **statut renvoyé par l'origine** (401/403/407/429/451/503) ou un marqueur de captcha dans le corps — donc une requête que l'origine a **déjà reçue et traitée**. Le commentaire `:424-425` (« Non-idempotent: a single attempt only ») ne décrit que le chemin streamé et est contredit par le chemin bufferisé.

**Scénario** : `POST /orders` (< 1 Mio) → l'origine répond 429 → le gateway re-POST via un second proxy → deux commandes créées.

**Correctif** : ne retenter que les méthodes idempotentes (GET/HEAD/PUT/DELETE/OPTIONS), ou exiger un en-tête explicite d'opt-in. **Effort : S.**

## A9 — Suppression d'un proxy pendant un tunnel → 500 injecté dans le flux TLS · **Moyenne** · A

`app/gateway_http.py:330-335` : dans le `finally` du tunnel, `await self.pool.mark_success(chosen.id, 0)` lève `KeyError` si le proxy vient d'être supprimé (`pool.py:223-227`). L'exception remonte au handler générique (`:261-264`) qui écrit `HTTP/1.1 500 …` dans un socket transportant déjà `200 Connection Established` et du TLS. `SPEC.md:263` prescrit explicitement de laisser la connexion se terminer. Le chemin SOCKS5 est moins grave (pas de REP émis) mais non géré. À l'inverse, `health_checker.py:142-146` gère correctement ce cas.

**Correctif** : englober l'appel dans un `suppress(KeyError)`. **Effort : S.**

## A10 — Identifiants SOCKS5 ré-encodés dans une URL · **Moyenne** · A

`app/gateway_http.py:345-352` et `app/gateway_socks.py:267-274` construisent `f"socks5://{username}:{password}@{host}:{port}"` avec le mot de passe déchiffré brut. `python_socks.parse_proxy_url` applique `urlparse` + `unquote` :

- `p%41ss` → devient silencieusement `pAss` : mauvais identifiants, proxy inutilement brûlé ;
- un mot de passe contenant `/ ? # [` — tous légaux d'après `parser.py:15` — lève `ValueError`, absent des tuples capturés (`gateway_http.py:302`, `gateway_socks.py:246-254`) → 500 par CONNECT, ou fermeture sans REP côté SOCKS5.

Les messages d'exception **ne contiennent pas** le mot de passe (vérifié) : robustesse, pas fuite. Les chemins aiohttp passent les identifiants en kwargs et sont immunisés.

**Correctif** : `urllib.parse.quote(password, safe="")`, ou contourner le parsing d'URL. **Effort : S.**

## Constats mineurs du data-plane

| ID | Constat | Fichier | Statut |
|---|---|---|---|
| A11 | `_read_headers` n'attrape pas `LimitOverrunError` → 500 au lieu de 400 sur une ligne d'en-tête > 64 Kio | `gateway_http.py:119` | A |
| A12 | Aucune réponse transmise ne porte `Connection: close`, alors que le gateway ferme toujours après une requête → pipelining client silencieusement perdu | `gateway_http.py:265-271`, `:582-611` | A |
| A13 | Le `Host` du client est transmis tel quel même s'il diffère de l'autorité de l'URI absolue | `gateway_http.py:416-419` | A |
| A14 | Le retry CONNECT n'a pas d'ensemble `tried` (contrairement à `_handle_forward`) → re-sélection du même proxy brûlé ; 503 au lieu de 502 si quarantaine en cours de boucle | `gateway_http.py:290-313` | A |
| A15 | `write_eof()` appelé sans `drain()` dans le `finally`, y compris sur chemin d'erreur | `gateway_http.py:150-153` | A |
| A16 | `UnicodeEncodeError` possible en ré-encodant les en-têtes de réponse en `latin-1` → 500 sans retry ni `mark_fail` | `gateway_http.py:86-93` | A |
| A17 | Fenêtre de fuite de socket : branche SOCKS5 de `aiohttp_socks.open_connection` non protégée par `try/finally` sous annulation `wait_for` | `gateway_http.py:297`, `gateway_socks.py:241` | A |

---

# 4. Cycle de vie santé, scoring et rotation

## B1 — Un proxy qui rate son premier check est bloqué « unknown » à vie · **Élevée** · V

`app/pool.py:344` : `status = "alive" if ok else ("quarantine" if failures >= 3 else old.status)`. Un proxy neuf (`unknown`) qui échoue une fois **reste** `unknown`, mais voit son `last_checked_at` renseigné.

`app/health_checker.py:271` ne retient que `proxy.status == "unknown" and proxy.last_checked_at is None`. Après ce premier échec, le proxy ne satisfait plus **aucune** des trois branches de `due()` (ni unknown-neuf, ni alive, ni quarantaine expirée) : il sort définitivement de la boucle de fond, y compris après redémarrage (`Pool.load` restaure `last_checked_at`), et reste exclu de `active()`. Seul un « tester tout / tester un » manuel le récupère. C'est une violation directe de la mission : « les proxies brûlés sont auto-quarantinés sans action manuelle ».

**Corroboration par les données réelles** : 74 des 100 proxies de `data/proxies.db` sont exactement dans cet état (`unknown`), 18 en quarantaine, 8 morts. Les 74 correspondent aux 74 premiers échecs 407 enregistrés à la seconde de l'import.

**Correctif** : inclure `unknown` dans `due()` (avec une échéance), ou passer à `quarantine` dès le premier échec. **Effort : S.**

## B2 — Les tunnels CONNECT enregistrent une latence de 0 · **Moyenne** · V

`app/gateway_http.py:335` et `app/gateway_socks.py:147` appellent `mark_success(chosen.id, 0)`. Dans `_outcome` (`pool.py:349-351`), ce `0` entre dans la même fenêtre de 20 mesures que les checks de santé et devient `latency_ms = median(...)`. Vingt CONNECT suffisent à écraser toutes les vraies latences.

Conséquence en cascade : `latency = 0` → `penalty = 0` (`pool.py:42-43`) → `health_score` atteint son maximum théorique → le proxy apparaît parfait dans le dashboard (0 ms), son poids dans la sélection `weighted` est gonflé, et l'historique SPEC §1/§2 (« médiane des 20 dernières *vérifications* ») est corrompu. `ARCHITECTURE.md:110` prétend au contraire que CONNECT enregistre `latency_ms=312`.

**Correctif** : ne pas alimenter l'historique de latence depuis les tunnels (compteur de succès séparé). **Effort : S.**

## B3 — `mark_fail` ignore la raison · **Faible** · V

`app/pool.py:308-310` : le paramètre `reason` n'est jamais utilisé. Un motif classé par le gateway (« HTTP 429 », « Body marker: captcha ») n'est enregistré nulle part — ce qui est cohérent avec la règle « ne jamais persister d'exception brute », mais perd toute observabilité sur les causes de brûlure.

## B4 — Round-robin biaisé · **Faible** · A

`app/rotator.py:102-104` : la sélection est indexée sur le tuple `active` recalculé à chaque appel. Si le proxy précédemment choisi sort du pool (ce que B1 rend fréquent), `previous = -1` → `active[0]` est choisi de nouveau. La répartition n'est pas uniforme en pratique.

## B5 — Le mode sticky dégénère · **Faible-moyenne** · V

`app/gateway_http.py:621` (et `gateway_socks.py:320`) codent `client_ip="127.0.0.1"` en dur : la clé SPEC §5 `(client_ip, domaine)` se réduit au domaine. De plus, `rotator.py:94-101` remappe la sélection dès que le proxy mémorisé quitte `active()` — ce qui, combiné à B1, peut survenir bien avant les 900 s de `STICKY_TTL_S`.

## B6 — `QUARANTINE_MINUTES` est ignoré · **Moyenne** (configuration trompeuse) · V

Présent dans `.env`, `.env.example:26` et `SPEC.md:213`, mais **absent de `app/config.py`** (vérifié par grep). La durée est codée en dur : `pool.py:348` `now + timedelta(minutes=10)`, valeur par défaut `minutes=10` en `:312`. Comme `Settings` utilise `extra="ignore"`, la ligne est acceptée sans erreur et silencieusement inopérante. Régler 60 n'a aucun effet.

## B7 — 3 des 5 signaux de brûlure SPEC §6 ne sont jamais appelés en production · **Moyenne** · V

`BurnDetector.classify` implémente statut, timeout, erreur de connexion et pic de latence (`burn_detector.py:33-52`). Mais les **seuls** appels de production passent `status_code` et `body` : `gateway_http.py:554` et `:571`. `elapsed_ms`, `median_latency_ms` et `error=` n'apparaissent nulle part ailleurs que dans `burn_detector.py` et son test unitaire. Le gateway intercepte lui-même `TimeoutError`/`ClientError` et appelle `mark_fail(id, "upstream_error")` sans jamais classer.

Donc `README.md:51` (« status + content + timeout + latency spike »), `CHANGELOG.md:18` et `TASKS.md:47-48` décrivent un comportement que le système en marche n'a pas.

**Correctif** : brancher les signaux restants, ou corriger la documentation. **Effort : S (doc) / M (branchement).**

## B8 — Suppression de proxy sans attendre la fin des tunnels · **Moyenne** · A

`ARCHITECTURE.md:161-163` et `SPEC.md:263` imposent de laisser les tunnels actifs se terminer avant une suppression dure. Rien ne l'implémente : `pool.remove` (`pool.py:292-303`) et `clear_all` (`:200-214`) suppriment immédiatement, ce qui provoque aussi le bug A9.

---

# 5. Sécurité et secrets

Le bilan est ici **majoritairement positif** — c'est la partie la plus solide du projet. Je détaille d'abord ce qui est réellement bien fait, puis les écarts.

## 5.1 Ce qui est vérifié comme solide

- **Chiffrement au repos réel.** PBKDF2-HMAC-SHA256, 480 000 itérations, salt aléatoire de 16 octets créé en `O_EXCL` mode 0600 (`app/salt.py:14-25`), clé Fernet dérivée, aucune clé de repli ni valeur codée en dur (`app/crypto.py:17-24`). `master_secret` est un `SecretStr` **obligatoire**, avec validateur rejetant vide et `CHANGE_ME_NOW` (`config.py:50-55`), revérifié dans `derive_key` (`crypto.py:19-20`) ; un déchiffrement qui échoue produit un message statique (`crypto.py:39-41`).
  **Vérification empirique sur la base réelle** : 100/100 lignes contiennent des jetons Fernet bien formés (préfixe `gAAAAABq`), et un balayage brut de `proxies.db` + `proxies.db-wal` à la recherche de motifs `scheme://user:pass@` ne retourne **aucune** occurrence.
- **Aucune fuite d'identifiants dans les canaux de sortie.** `password_enc` est un `SecretBytes` avec `Field(exclude=True, repr=False)` (`models.py:46`) : `model_dump()`, `model_dump_json()`, `repr()` et `__dict__` l'omettent tous. Le WebSocket construit ses trames par sélection explicite de champs (`routes/events.py:16-35`) et retire même `exit_ip`/`checked_at`. Le handler 422 global remplace intégralement le détail de FastAPI par `{"detail": "Invalid request"}` (`main.py:151-154`), et `hide_input_in_errors=True` est posé sur les modèles sensibles. Les messages de `ProxyParseError` sont statiques et n'incluent jamais l'entrée (`parser.py:21-22`).
  **Vérification empirique** : aucun message d'erreur en base ne contient d'identifiant — uniquement `HTTP 407: proxy authentication refused`, `Timeout`, `Proxy connection failed`.
- **Secrets non versionnés.** `.env`, `*.env`, `data/.salt`, `data/proxies.db*` sont ignorés ; `git ls-files` ne remonte que `.env.example`. Le `.env` réel est en 0600.
- **Fail-closed réel.** Aucun chemin ne retombe en connexion directe : `_pick` retournant `None` donne `503 Pool Empty` (`gateway_http.py:294,461,476`) ou un REP d'échec SOCKS5 (`gateway_socks.py:136`). `trust_env=False` sur toutes les sessions sortantes empêche la récupération d'un proxy d'environnement.
- **Protection contre la fuite DNS réelle.** `rdns=True` dans le health checker (`health_checker.py:63-69`) et le connecteur SOCKS5 du forwarding (`gateway_http.py:522-528`) ; l'ATYP domain est transmis non résolu (`gateway_socks.py:202-207`) ; la cible CONNECT est envoyée sous forme authority (`:364-368`).
- **Hygiène des en-têtes.** Retrait des hop-by-hop, y compris les jetons nommés dans `Connection` (`:65-74`), ajout de `Via`, `Proxy-Authorization` client jamais transmis à l'origine.
- **Intégrations réellement opt-in.** Géolocalisation désactivée par défaut (`config.py:39`), retour immédiat avant tout accès réseau si désactivée (`geolocation.py:82-83`) ; le jeton dichvusocks n'est transmis que dans un en-tête `Authorization: Bearer` vers une base HTTPS (`providers/dichvusocks.py:143-147`) et n'est jamais renvoyé par l'API (`routes/providers.py:143,157`).

## C1 — Aucune authentification ; export en clair des mots de passe · **Élevée si exposition LAN** · V

Un grep de `add_middleware|TrustedHost|HTTPBasic|Depends(.*auth` sur `app/` ne retourne **rien** : il n'existe ni authentification, ni `TrustedHostMiddleware`, ni contrôle d'`Origin`, ni jeton CSRF.

`app/routes/proxies.py:88-105` expose `GET /api/proxies/export`, qui appelle `pool.export_lines` (`pool.py:143-161`) : celui-ci **déchiffre** chaque mot de passe (`:153`) et les concatène en `scheme://user:pass@host:port` (`:158`). La fonctionnalité est explicitement demandée (export des proxies vivants) — le problème n'est pas l'export, c'est l'absence de toute barrière devant.

`app/main.py:131-132` se contente d'un `logger.warning` pour un bind non-loopback, puis `:175` lie uvicorn à `settings.bind_host` sans autre contrôle.

**Scénarios** :
- Avec `BIND_HOST=0.0.0.0` (explicitement supporté) : un simple `GET` non authentifié depuis n'importe quelle machine du LAN récupère tous les identifiants de proxies.
- Sur loopback : l'absence de validation du `Host` laisse une page web atteindre l'API par DNS rebinding, et les POST destructeurs ou dépensiers (`/api/pool/clear-all`, `clear-dead`, `/api/providers/dichvusocks/import`) acceptent des POST de formulaire cross-site sans préflight — une page visitée peut vider le pool ou consommer des crédits fournisseur.

**Circonstance atténuante** : le `.env` réel est en `BIND_HOST=127.0.0.1`, donc l'exposition large n'est pas active aujourd'hui. `GET /api/proxies/export` n'a **aucune couverture de test** (`grep -rl "export" tests/` → rien).

**Correctif** : authentification minimale sur l'UI/API d'admin, validation du `Host`/`Origin`, et au minimum un refus explicite du bind non-loopback sans second opt-in. **Effort : M.**

## C2 — Géolocalisation active dans la configuration réelle, en HTTP en clair · **Moyenne** · V

Le défaut est bien désactivé (`config.py:39`), mais l'URL fournisseur par défaut est `http://ip-api.com/json/{ip}...` — **http, pas https** (`config.py:40`). Et le `.env` réel contient `GEOLOCATION_ENABLED=true` sans surcharge d'URL (vérifié).

La base contient **70 entrées `geo_cache`** : 70 IP de sortie ont déjà été transmises à un tiers, en clair. `geolocation.py:156-167` n'envoie que l'IP (pas d'identifiants, `trust_env=False`, redirections désactivées) et `aiohttp.ClientError/TimeoutError/OSError` sont avalés avec un log statique (`:177-179`). Mais `geolocation.py:181-202` fait confiance à la réponse non authentifiée, la met en cache, puis `pool.py:102-113` et `rotator.py:57-63` s'en servent pour **filtrer les candidats à la rotation**.

**Double impact** : (a) les IP de sortie de tous les proxies sont divulguées en clair à un tiers ; (b) un MITM sur du HTTP peut forger la réponse, empoisonner le cache et orienter le trafic de l'utilisateur vers le pays de son choix via le filtre pays. À noter que `HEALTH_PROBE_URL` est en https par défaut : l'URL géo est l'exception.

## C3 — `/api/geo/{ip}` relaie n'importe quelle IP publique · **Faible-moyenne** · A

`app/routes/geo.py:12-25` accepte une IP arbitraire de l'appelant et l'envoie au fournisseur ; seules les plages privées/boucle locale/réservées sont rejetées (`geolocation.py:88-89`). Quand le service est actif, c'est un relais de requêtes tiers non authentifié.

## C4 — Aucun garde-fou automatisé pour la règle « ne jamais logger d'identifiants » · **Moyenne** (fausse assurance) · V

`docs/SECURITY.md:26` affirme que le masquage `user:***` est « Enforced by lint rule » ; `:51` impose que chaque logger appelle `app.log.mask()` ; `:53` référence `pytest tests/security/` ; `:33` et `:54` exigent `pip-audit` en CI.

Vérifié : `app/log.py` **n'existe pas**, `tests/security/` **n'existe pas**, `grep -c audit .github/workflows/ci.yml` retourne **0**, et aucune règle ruff personnalisée ne détecte un log d'identifiant. La règle tient aujourd'hui par discipline et par quelques bons tests — pas par un filet automatique.

**Note** : `aiohttp.BasicAuth` est un namedtuple dont le `repr` contient le mot de passe. Il est construit avec le secret déchiffré en `gateway_http.py:531` et `health_checker.py:72`, mais uniquement conservé en variable locale et jamais journalisé. Fuite **latente** : un futur `logger.debug("...", proxy_auth)` suffirait.

## C5 — Base et WAL lisibles par tous · **Faible** (défense en profondeur) · V

Relevé sur disque : `data/.salt` est en `-rw-------` (0600), mais `data/proxies.db`, `-wal` et `-shm` sont en `-rw-r--r--` (0644). `app/db.py:67` ne fixe aucun mode. L'encryption protège les blobs et `.env` est en 0600, donc ce n'est pas une compromission — mais sur une machine partagée, tout utilisateur local peut lire le magasin d'identifiants.

## C6 — Autres points de vigilance

| ID | Constat | Fichier | Statut |
|---|---|---|---|
| C6.1 | `routes/providers.py:33,42,61,79,92` renvoient `str(exc)` au client — messages aujourd'hui statiques, mais passe-plat non filtré | `routes/providers.py` | A |
| C6.2 | `_format_reason` renvoie le message aiohttp dans le corps de la réponse 502 (`:316,464,497`) | `gateway_http.py:647-649` | A |
| C6.3 | Données de requête client journalisées verbatim (`:108,111,128` → `logger.warning`/`exception`) — entrée non assainie dans les logs | `gateway_http.py` | A |

---

# 6. Tests

## 6.1 Points forts réels (vérifiés)

- **Un seul mock dans tout le dépôt** : `tests/test_parser_bulk.py:54`, utilisé en espion de comptage avec `wraps=`. Partout ailleurs : vraies bases SQLite par test, vrais serveurs aiohttp/TCP sur loopback.
- **Fixture anti-fuite** : `conftest.py:27-40` échoue si une tâche asyncio survit au teardown.
- **Test de schéma contre la spec** : `test_migrations.py:19-26` parse `SPEC.md` et diffe le schéma construit contre lui.
- **TLS réel** : `test_health_tls.py` fabrique une AC éphémère, vérifie que le tunnel CONNECT est authentifié **et** qu'un certificat non approuvé produit « TLS certificate verification failed ».
- **DNS** : `test_health_socks.py:31` vérifie ATYP=domain (pas de résolution locale).
- **Horloges injectées** pour la quarantaine et la décroissance.
- **La règle 7 de `CLAUDE.md` est réellement satisfaite** : `test_parser.py:154-164` vérifie `parse_line(serialize(parse_line(x))) == parse_line(x)` sur 300 exemples, avec une variante d'identité littérale plus forte en `:124-128`.

## E1 — Deux tests cités comme preuve du retry ne testent rien · **Élevée** · V

`tests/test_gateway_http.py:525-546` (`test_retry_on_upstream_connect_error_then_success`) ne provoque **aucune erreur** : il contient des commentaires de brouillon, une boucle morte `for sock in asyncio.all_tasks(): break  # dummy loop to appease linters` (`:533`), des variables assignées puis jetées, puis envoie une requête normale et asserte `200`.

`tests/test_gateway_http.py:549-571` (`test_retry_on_burn_status_then_success`) ne fait répondre **429 à aucun upstream** : la boucle `:553-556` est un `pass`, et le test asserte un `200` normal plus un incrément de `requests_total`.

Les deux passeraient si l'on supprimait entièrement le retry. Or `TASKS.md:71-72` les cite comme preuve (« 4 new tests cover retry success/exhaustion/status-pass-through »). Seul `test_retry_exhausted_returns_502` (`:573`) teste réellement le comportement.

C'est le constat le plus dommageable pour la confiance : il montre que la revendication de couverture n'a pas été confrontée aux tests eux-mêmes.

## E2 — `test_no_retry_on_2xx` faible · **Faible** · V

`tests/test_gateway_http.py:589-600` est une copie de `test_successful_forward_marks_success` (`:461-472`) et ne compte jamais les tentatives amont : il ne pourrait pas détecter que le gateway se met à retenter les 2xx.

## E3 — Le fournisseur dichvusocks : 403 lignes, zéro test · **Moyenne** · V

`grep -rl "dichvusocks\|provider_store" tests/` ne retourne **aucun** fichier. Sont donc non testés : `app/providers/dichvusocks.py` (178 lignes), `app/routes/providers.py` (157) et `app/provider_store.py` (68) — y compris l'appel `view()` qui **consomme des crédits** et le magasin de jetons chiffrés.

## E4 — Aucun test de corps de requête · **Moyenne** · A

Aucun POST/PUT avec corps ni envoi chunked dans `test_gateway_http.py`, alors que `TASKS.md:72` documente précisément ce comportement (bufferisation ≤ 1 Mio, single-shot au-delà). C'est aussi ce qui a laissé passer A3, A4 et A8.

## E5 — Couverture fictive d'un contrat jamais émis · **Faible** · A

`tests/test_websocket.py:69-73` appelle lui-même `pool.publish_gateway_event` puis asserte la trame qu'il vient de provoquer. Aucun code de production n'émet jamais cet événement (`pool.py:175-176` n'est appelé que par ce test), alors que `docs/API.md:146` le documente.

## E6 — Sensibilité à la charge / courses · **Moyenne** · A

- `test_health_checker.py:147-157` enchaîne 3 sondes avec `delay = 3.2` dans un budget de 15 s (~10 s de temps réel).
- `_wait_idle` (`test_gateway_http.py:376-382`) rend la main dès `active_conns == 0`, ce qui survient dans le `finally` **avant** la fin de `mark_success` : les assertions sur `success_count` (`:461-472`, `:589-600`) sont des courses. Plusieurs tests compensent par des `sleep` fixes (`test_proxy_api.py:153,158` ; `test_main.py:152` ; `test_gateway_socks.py:336-339`).

## E7 — Test rouge local · **À vérifier hors sandbox** · V

`tests/test_start_command.py::test_finder_restarts_only_same_project` échoue chez moi en 0,56 s. Cause identifiée : `/bin/ps` est refusé par le sandbox (`Operation not permitted`), donc `start.command` ne peut énumérer aucun processus et ne tue rien. Le script retourne 0 et relance : dégradation silencieuse, mais cohérente. **À noter tout de même** : si `ps` échoue en usage réel, le launcher ne signale rien et l'ancienne instance continue d'occuper le port 8080.

## E8 — Nuance sur « property-based » · **Faible** · A

Tous les tests Hypothesis utilisent `derandomize=True, database=None` : corpus fixe rejoué à l'identique. Déterministe (bon pour la CI) mais non exploratoire d'une exécution à l'autre. `test_repository_properties.py` est un test d'identité insert→list→reopen plutôt qu'une propriété.

---

# 7. Documentation : écarts entre promesses et réalité

## F1 — Preuve de rotation jamais obtenue, critère affaibli · **Élevée** (au regard du cahier des charges) · V

`CLAUDE.md` (« When done » #3) exige `curl -x http://127.0.0.1:8888 https://api.ipify.org` **trois fois avec trois IP de sortie différentes**. `TASKS.md:75-76` coche T5.4 en affaiblissant le critère à « au moins deux IP distinctes » et enregistre « Live proof SKIPPED ». `TASKS.md:93` et `CHANGELOG.md:35-37` admettent que le smoke sort en 1 faute d'upstreams. `docs/BROWSER_SETUP.md:66` promet encore « three different IPs » alors que `README.md:164-165` dit le contraire.

**La fonctionnalité centrale du produit n'a donc jamais été démontrée de bout en bout.**

## F2 — Documentation périmée ou fictive · **Moyenne** · A (V pour `F2.4`, `F2.6`)

| ID | Écart | Référence |
|---|---|---|
| F2.1 | `docs/API.md:118-119` affiche `"bound": null` et des compteurs à zéro ; `:125-126` décrit les listeners comme des placeholders de phase 4 alors qu'ils démarrent (`main.py:87-90`) | `docs/API.md` |
| F2.2 | `docs/API.md:146` documente `gateway_event` — événement jamais émis en production | `docs/API.md`, `pool.py:175` |
| F2.3 | `docs/API.md` omet ~8 endpoints livrés (`/export`, `/pin`, `/unpin`, `/api/pool/clear-all`, `/api/geo/{ip}`, 5 routes providers, `/parametres`, `/healthz`) alors que `CLAUDE.md` le présente comme exhaustif | `docs/API.md` |
| F2.4 | `CHANGELOG.md:38-40` et `TASKS.md:94` déclarent la dette « gateway affiche des adresses configurées et un label statique » — faux : `runtime.py:54-65` calcule l'état réel et le template rend `bound`/`active_conns`/`requests_total` | `CHANGELOG.md` |
| F2.5 | `ARCHITECTURE.md:185` (« retries remain T5.2 »), `:228-230` (« listeners remain Phase 5 »), `:72` (« per-conn asyncio.Lock » inexistant), `:161-163` (drain des tunnels avant suppression, non implémenté) | `ARCHITECTURE.md` |
| F2.6 | `docs/SECURITY.md:26,33,51,53,54` décrit un lint rule, `app.log.mask()`, `tests/security/` et un `pip-audit` CI — **aucun n'existe** (vérifié) | `docs/SECURITY.md` |
| F2.7 | `CHANGELOG.md:7` résume « 23 commits » (`977b3aa`→`70e79b5`) : le dépôt en compte 52 ; les 24 commits postérieurs (géolocalisation, provider, rotation minutée, pin/export, split dashboard/paramètres) en sont absents | `CHANGELOG.md` |
| F2.8 | `README.md:45-54` omet export/pin/géolocalisation/rotation minutée/provider ; `:135-139` liste 4 fichiers de test sur 34 | `README.md` |
| F2.9 | `PRODUCT.md:13` (« Gateway protocols unimplemented until Phase 5 ») et `DESIGN.md:3-5` (maquette claire) contredisent `styles.css:3,41` (« Dark topbar », `--topbar-bg:#0b1324`) | `PRODUCT.md`, `DESIGN.md` |
| F2.10 | `TASKS.md:84` coche T6.3 alors que `:85` admet que la CI hébergée n'a jamais tourné | `TASKS.md` |

---

# 8. Architecture et dette structurelle

## G1 — Inversion de couches · **Moyenne** · V

`ARCHITECTURE.md:58` (« API … Does NOT: business logic ») et `:61` (« arrows go down, never up »). Or les routes importent la couche UI, vérifié par grep :

```
app/routes/ui.py:7        from app.ui.rendering import render
app/routes/proxies.py:12  from app.ui.rendering import feedback, render
app/routes/pool.py:9      from app.ui.rendering import feedback
```

S'y ajoutent `routes/gateway.py:10` qui importe `Services` depuis `routes.proxies` (route↔route), `routes/gateway.py:32` qui exécute lui-même la sonde, et `routes/providers.py:64-116` qui fait de l'orchestration d'import dans la couche transport.

## G2 — Couplages et duplications · **Faible-moyenne** · A

- `gateway_socks.py:277` importe les helpers **privés** `_basic_auth` et `_read_headers` de `gateway_http`.
- `_pipe` dupliqué mot pour mot (`gateway_http.py:140-153` vs `gateway_socks.py:325-338`).
- Handshake CONNECT amont dupliqué (`gateway_http.py:355-396` vs `gateway_socks.py:277-315`).
- `_pick` dupliqué (`gateway_http.py:619-622` vs `gateway_socks.py:317-322`).

## G3 — Fichiers trop gros, cohésion faible · **Moyenne** · V

`app/gateway_http.py` : **756 lignes, 17 % de `app/`**, et mélange parsing de requête, tunneling CONNECT, forwarding en URI absolue, politique de brûlure, politique de retry et un décodeur chunked écrit à la main (c'est précisément là que se concentrent A3, A4, A6).

`app/pool.py` (382 lignes) combine write-through persistance, scoring, fanout d'événements, épinglage, activité gateway, observation d'IP de sortie et cache pays.

## G4 — Code mort et contrat non tenu · **Faible** · A

- `repository.upsert_proxy` (`repository.py:41`) appelé uniquement par les tests.
- `Pool.publish_gateway_event`/`GatewayEvent` : production jamais appelée.
- `idx_proxies_status_score` (`migrations.py:22`) créé mais utilisé par aucune requête.
- Branches timeout/connexion/pic de latence du burn detector (voir B7).
- `mark_fail` perd la raison (voir B3).

## G5 — Dérive de schéma hors migrations · **Moyenne** · V

Le système versionné ne connaît qu'une migration (`migrations.py` : `proxies` + `proxy_checks`, `user_version = 1`). Deux tables sont créées **en dehors**, avec leurs propres `commit()` sur une connexion en autocommit (`db.py:67`) :

```
app/geolocation.py:66     CREATE TABLE IF NOT EXISTS geo_cache
app/provider_store.py:22  CREATE TABLE IF NOT EXISTS provider_tokens
```

Ni l'une ni l'autre n'apparaît dans `SPEC.md §9`, et `test_migrations.py` prouve justement que la migration 001 correspond exactement à la spec — donc ces tables sont une dérive non gérée. Au passage, la table d'ARCHITECTURE.md ne mentionne pas non plus `config.py`, `runtime.py`, `events.py`, `activity.py`, `geolocation.py`, `provider_store.py`, `providers/`, `gateway_probe.py` ni `routes/{ui,events,geo,providers}.py`, soit ~1 000 lignes hors du cadre documenté.

---

# 9. Hygiène, dépendances, CI

## H1 — 49 vulnérabilités connues dans 5 paquets figés · **Élevée** · V

`pip-audit -r requirements.txt` : **49 vulnérabilités connues dans 5 paquets**.

| Paquet | Version figée | Exemples de correctifs requis |
|---|---|---|
| `aiohttp` | 3.10.11 | 3.13.3 → 3.14.3 (17 avis) |
| `cryptography` | 43.0.1 | 44.0.1 → 49.0.0 (6 avis) |
| `starlette` | 0.38.6 | 0.40.0 → 1.3.1 (7 avis) |
| `anyio` | 4.11.0 | 4.14.2 (2 avis) |
| `pytest` | 8.3.3 | 9.0.3 (1 avis) |

`pip-audit` figure pourtant dans `requirements.txt:24` mais n'est exécuté nulle part (`grep -c audit .github/workflows/ci.yml` → 0). Le commit « fix(gateway): handshake timeout + CVE bumps » montre que le sujet a été traité une fois, puis figé. Les avis `aiohttp` et `starlette` concernent du code qui traite des données non fiables (réponses de proxies, requêtes navigateur), donc l'impact n'est pas théorique.

## H2 — La discipline de commit casse après le gate v1 · **Moyenne** · A

52 commits, un seul auteur, 2026-10-06 → 2026-10-09. Les ~28 premiers commits, ordonnés selon `TASKS.md`, touchent chacun `app/` **et** `tests/` — cohérent avec le flux TDD revendiqué. Puis **14 commits ajoutent ou modifient du code de production sans aucun changement de test** (dont `223ddf4`, `4a39241`, `eb6b397` pour le provider, `5eade4b` pour la géolocalisation) : c'est exactement là qu'ont atterri les 403 lignes non testées.

## H3 — Le smoke test n'a jamais passé · **Élevée** (preuve d'intégration) · V

`scripts/smoke_gateway.sh` existe et est correctement conçu (attente bornée, sortie limitée aux IP validées, nettoyage du groupe de processus, pas de repli direct). Mais `TASKS.md:93` et `CHANGELOG.md:35-37` indiquent qu'il sort en 1 faute de `.env`/upstreams. Aucune preuve de rotation n'existe donc.

**Corroboration par les données réelles** : `data/proxies.db` contient 100 proxies, 1 374 checks enregistrés, dont **1 365 en échec** (99,3 %) et **9 en succès**, avec seulement **2 IP de sortie** distinctes jamais observées. Le proxy 1110 a renvoyé `200` de 07:55 à 08:27 le 09/10, puis `407` en boucle ensuite avec les **mêmes identifiants chiffrés** : c'est une expiration côté fournisseur, pas un bug de déchiffrement. En revanche, la conséquence est visible : le pool est en pratique inutilisable, 74 proxies sont bloqués en `unknown` (B1), et la dernière réussite date du 09/10.

## H4 — Divers hygiène

| ID | Constat | Statut |
|---|---|---|
| H4.1 | `requirements.txt:16-17` renvoie à `requirements-dev.txt`, qui n'existe pas (vérifié) | V |
| H4.2 | `.github/workflows/ci.yml` correspond bien à ce que `TASKS.md:84` décrit (matrice ubuntu/macos/windows, py3.11, `pytest -q`, `ruff check app tests`, `mypy app`) — mais n'a **jamais tourné** : `git remote -v` est vide. Windows n'a donc jamais été validé. | V |
| H4.3 | `tests/test_dependencies.py:20` asserte l'import de `click`, dépendance transitive d'uvicorn, jamais importée par `app/` | A |
| H4.4 | Hors-scope assumé mais non documenté : le provider dichvusocks (403 lignes, 5 endpoints, magasin de jetons chiffré, dépendance sortante vers dichvusocks.net) n'apparaît dans **aucune** ligne de `TASKS.md`, contredisant la règle 8 de `CLAUDE.md` (« No scope creep »). C'est aussi un second type de trafic sortant au-delà de la règle 4. | A |
| H4.5 | Artefacts locaux non suivis : `api documentation - dichvusocks.net.pdf` (284 Ko), `.DS_Store`, `data/proxies.db-wal` (4,2 Mo). `git status` est propre par ailleurs. | V |

---

# 10. Constats réfutés

**R1 — « Le pool se vide après ~55 s d'inactivité et renvoie 503 alors que les proxies sont sains » : FAUX.**

Un des audits concluait que `freshness = exp(-age_minutes/60)` combiné à `active() >= 40` et à un intervalle de check de 300 s vidait le pool après environ 55 secondes. C'est une erreur d'arithmétique : `age_minutes` étant déjà en minutes, la constante de temps vaut **60 minutes**, pas 60 secondes.

Calcul vérifié avec la formule exacte de `app/pool.py:37-45` (ratio 1,0, latence 200 ms) :

| Âge depuis le dernier check | Score | Actif (≥ 40) |
|---|---|---|
| 60 s | 95,4 | oui |
| 300 s (intervalle de check) | 89,2 | oui |
| 600 s | 82,1 | oui |
| 1 800 s | 58,8 | oui |
| 3 186 s (~53 min) | 40,0 | limite |

La décroissance par intervalle de 5 minutes n'est que de ~8 %, et le health checker rafraîchit `last_checked_at` à chaque sonde. **Aucun correctif n'est nécessaire ici** — à ne pas poursuivre.

*C'est la raison d'être de la colonne « statut » : un audit non revérifié propage facilement ce genre d'erreur.*

---

# 11. Ce qui est solide (à ne pas casser)

1. **Chiffrement et manipulation des secrets** — réels, correctement câblés, vérifiés en base : 100/100 jetons Fernet, 0 identifiant en clair dans `.db`/`.wal`, `SecretBytes` structurellement inexportable, erreurs statiques, handler 422 générique.
2. **Fail-closed intégral du data-plane** — aucune session sortante sans proxy (hors les trois usages documentés : géo opt-in, client fournisseur, sonde du listener local). `503 Pool Empty` / REP d'échec, jamais de connexion directe.
3. **Protection contre la fuite DNS** — `rdns=True` partout, ATYP domain transmis non résolu, cible CONNECT en forme authority.
4. **Concurrence du pool** — toutes les mutations passent par `Pool._mutate` sous un `asyncio.Lock` unique (`pool.py:233-244`), lectures sans verrou sur un tuple immuable échangé atomiquement (`:229-231`), commit SQLite et échange du snapshot dans le même pas de boucle événementielle (`:368-375`) : pas de lecture déchirée, pas de mise à jour perdue, annulation protégée par `shield`.
5. **Formule de score conforme à `SPEC.md §4`** (`pool.py:37-45`), y compris le clamp de latence et le garde sur les dates futures ; les deux bornes « 3 échecs → quarantaine 10 min » (`:344-348`) et « score < 10 après retest → mort » (`:365-366`) correspondent à la spec.
6. **Hygiène du health checker** — sémaphore + timeout par sonde (`:45,104,116`), `trust_env=False`, déchiffrement hors boucle événementielle (`:107-111`), erreurs de configuration jamais imputées à un proxy (`:106`), `KeyError` de proxy supprimé géré (`:142-146`).
7. **Backpressure réelle dans les tunnels** — 64 Kio par `drain()` dans les deux sens, propagation du half-close.
8. **Balisage borné** — ligne de requête 8 Kio, bloc d'en-têtes 32 Kio, nmethods SOCKS ≤ 255, domaine ≤ 255, handshake sous `asyncio.timeout`.
9. **Qualité de la suite de tests** (hors E1-E6) — un seul mock, vraies ressources, détection de fuites de tâches, schéma confronté à la spec, TLS et DNS réellement testés.
10. **Honnêteté du `CHANGELOG.md`** — les limites de validation sont écrites noir sur blanc plutôt que maquillées.

---

# 12. Plan d'action priorisé

## Palier 1 — Correction (le produit ne tient pas ses promesses sans ça)

| # | Action | Constat | Effort |
|---|---|---|---|
| 1 | Indicateur « réponse déjà commencée » : plus aucun retry après le premier octet écrit au client, + test de non-régression (troncature en cours de corps) | A1, A2 | S |
| 2 | Inclure `unknown` dans `due()` (ou quarantaine dès le 1ᵉʳ échec), puis purge des 74 proxies bloqués en base | B1 | S |
| 3 | Ne retenter que les méthodes idempotentes | A8 | S |
| 4 | Rejeter CL+TE et les CL divergents ; valider `^[0-9a-fA-F]+$` et plafonner la taille de chunk | A3, A4 | S |
| 5 | Exclure les tunnels de l'historique de latence (compteur de succès séparé) | B2 | S |
| 6 | Supprimer ou écrire réellement les deux faux tests de retry ; corriger `TASKS.md:71-72` | E1 | S |

## Palier 2 — Sécurité et robustesse

| # | Action | Constat | Effort |
|---|---|---|---|
| 7 | Bump `aiohttp`/`cryptography`/`starlette`/`anyio`/`pytest` + étape `pip-audit` bloquante en CI | H1 | M |
| 8 | `quote(password, safe="")` sur les chemins tunnel SOCKS5 | A10 | S |
| 9 | Timeout d'inactivité sur le corps client ; répondre `100 Continue` ; plafond de connexions par gateway | A5, A6, A7 | M |
| 10 | Authentification minimale + validation `Host`/`Origin` sur l'admin ; refus explicite du bind non-loopback sans second opt-in ; décider du sort de `/api/proxies/export` | C1 | M |
| 11 | Passer l'URL géo par défaut en HTTPS et documenter l'impact ; envisager de désactiver la géo dans le `.env` réel | C2 | S |
| 12 | `suppress(KeyError)` dans le `finally` du tunnel | A9 | S |
| 13 | Créer `app/log.py` + un test qui échoue si un identifiant apparaît dans les logs, ou corriger `docs/SECURITY.md` | C4 | S-M |
| 14 | Mode 0600 sur `proxies.db`/`-wal`/`-shm` | C5 | S |

## Palier 3 — Rétablir la confiance dans les preuves

| # | Action | Constat | Effort |
|---|---|---|---|
| 15 | Brancher les signaux brûlure timeout/connexion/pic de latence, ou corriger `README.md:51`/`CHANGELOG.md:18` | B7 | S-M |
| 16 | Ajouter un champ `quarantine_minutes` (ou retirer la ligne de `.env.example`/`SPEC.md`) | B6 | S |
| 17 | Tests du corps de requête (POST/PUT/chunked) et smoke tests du provider dichvusocks | E3, E4 | M |
| 18 | **Avec 2 proxies vivants** : exécuter `scripts/smoke_gateway.sh` et coller la sortie réelle dans `CHANGELOG.md` | F1, H3 | S |
| 19 | Corriger `ARCHITECTURE.md` (`:72,161-163,185,228-230`), `docs/API.md` (`:118-146`, endpoints manquants), `CHANGELOG.md` (23→52 commits, dette UI périmée), `PRODUCT.md`, `DESIGN.md` | F2 | M |
| 20 | Enregistrer `geo_cache` et `provider_tokens` comme migration 002 | G5 | S |
| 21 | Supprimer le code mort (G4) et dédupliquer `_pipe`/handshake/`_pick` (G2) ; extraire le parsing HTTP de `gateway_http.py` | G2, G3, G4 | M-L |
| 22 | Documenter le provider dichvusocks dans `TASKS.md`/`README.md` (règle 8) ; supprimer la référence à `requirements-dev.txt` | H4.1, H4.4 | S |

---

# 13. Annexe — reproduction

## A1 : seconde réponse HTTP concaténée

```python
# upstream factice : annonce 1 Mo, envoie 100 octets, ferme
writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 1000000\r\n"
             b"Content-Type: text/plain\r\n\r\n" + b"A" * 100)
# puis, via le gateway (pool = 1 proxy, max_retry_per_request=2) :
#   GET http://example.invalid/x HTTP/1.1
# observer les octets reçus
assert data.count(b"HTTP/1.1 ") == 2   # ← échec : 200 partiel + 502 concaténé
```

## Vérifications ponctuelles

```bash
cd /Users/MacBookPro/Downloads/proxy-rotator
.venv/bin/python -m pytest -q                      # 281 passed, 1 skipped, 1 failed
.venv/bin/python -m ruff check app tests           # All checks passed
.venv/bin/python -m mypy app                       # Success: 34 source files
.venv/bin/python -m pip_audit -r requirements.txt  # 49 vulnérabilités / 5 paquets
.venv/bin/python -c "print(int(b'-1',16))"         # -1  → A4
grep -rn "Expect" app/                             # (aucun résultat) → A5
grep -rn "Semaphore\|Lock\|MAX_CONN" app/gateway_http.py app/gateway_socks.py   # (aucun) → A7
grep -rn "add_middleware\|TrustedHost\|HTTPBasic" app/                          # (aucun) → C1
ls app/log.py requirements-dev.txt tests/security  # (introuvables) → C4, H4.1
grep -rn "from app.ui" app/routes/                 # 4 hits → G1
git remote -v                                      # (vide) → H4.2
```

## Base de production (lecture seule)

```python
# sqlite3.connect("file:data/proxies.db?mode=ro", uri=True)
# proxies        : 100  (74 unknown / 18 quarantine / 8 dead ; 100 http)
# proxy_checks   : 1374 (1365 ko / 9 ok) ; 2 IP de sortie distinctes
# geo_cache      : 70   ;  provider_tokens : 0
# password_enc   : 100 % jetons Fernet ; 0 motif user:pass@ en clair dans .db/.wal
```

---

*Rapport produit par analyse statique et dynamique en lecture seule. Aucun fichier du projet n'a été modifié (un fichier de reproduction temporaire a été créé puis supprimé ; `git status` est identique à l'état initial).*
