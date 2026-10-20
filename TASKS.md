# TASKS.md — Prioritised backlog for v1 (MVP)

Work top to bottom. Check each box when done. One task = one commit.

---

## Phase 0 — Bootstrap

- [x] T0.1  `pip install -r requirements.txt` — macOS validated, Windows deferred → T6.3.
  - macOS / Python 3.11 installation and `pip check` pass; dependency-import test observed failing before installation and passing afterward (stdlib unittest runner used before pytest was installed). Windows validation is deferred to the T6.3 GitHub Actions matrix, as agreed with the user.
  - Prerequisite correction: annotate the existing `lifespan` return type to satisfy the mandatory mypy gate; no runtime behavior added.
- [x] T0.2  `pytest -q` passes (the bootstrap smoke test counts).
  - Both bootstrap tests pass; retain `tests/test_smoke.py`.
- [x] T0.3  `ruff check app tests` + `mypy app` pass on the real codebase.
  - Validated implicitly by T1.1 and its pre-commit checks.
- [x] T0.4  Both launchers provide dependency-free `--help` (exit 0); missing dependencies bootstrap automatically per updated T6.1/T6.2 requirements.
  - Original missing-dependencies/exit-0 wording superseded by the requested automatic bootstrap.

## Phase 1 — Core (pure)

- [x] T1.1  `app/parser.py` : `parse_line(str) -> ParsedProxy` + unit tests + property-based round-trip test (Hypothesis).
  - TDD: observed red tests before each implementation slice; 300 deterministic Hypothesis round trips cover schemes, credentials, IPv4/IPv6 and hostnames. Passwords use SecretStr, masked representations and credential-free parse errors; bulk blank/comment handling remains T1.2.
- [x] T1.2  `app/parser.py` : `parse_bulk(text) -> ImportReport` handles blanks, comments, duplicates, bad lines without raising.
  - Partial import, first-wins identity deduplication, 2000-physical-line cap; safe 1-based error line numbers. Hypothesis covers 200 mixed batches plus 200 arbitrary-text inputs. Accepted records are excluded from report JSON.
- [x] T1.3  `app/crypto.py` : Fernet encrypt/decrypt + key derivation (PBKDF2HMAC-SHA256, 480k iters, salt from `data/.salt`). Tests.
  - Pure crypto plus `app/salt.py` storage helper; independent KDF vector, Fernet interoperability/tamper tests, persisted-salt restart and POSIX permission checks. Safe errors carry EX_CONFIG=78 for startup integration in T4.1.
- [x] T1.4  `app/models.py` : Pydantic DTOs `Proxy`, `CheckResult`, `PoolStats`. No I/O.
  - Immutable validated snapshots, UTC-aware dates, bounded ports/scores/counters and strategy/status literals. `password_enc` is a SecretBytes field excluded from public serialization and repr; timestamps are supplied by callers.

## Phase 2 — Storage

- [x] T2.1  `app/db.py` : open aiosqlite connection, apply migrations, context manager.
  - Async connection ownership, foreign_keys=ON, WAL, autocommit, atomic versioned migrations and explicit batch transaction context. Real SQLite tests cover reopening, rollback/cancellation, idempotence and unsupported future versions.
- [x] T2.2  Migration 001 : tables from `SPEC.md §9`.
  - Exact schema checked against a reference database built from SPEC §9; user_version=1, idempotence, CHECK/UNIQUE/FK/cascade tests. Already-current migrations avoid writer locks so WAL readers remain available.
- [x] T2.3  Repository functions: `insert_proxy`, `upsert_proxy`, `list_proxies`, `delete_proxy`, `record_check`.
  - Async repository with bound values, encrypted credential writes, DTO returns and validated filters. Real-DB tests cover upsert, cascade deletion, 50-check retention, atomic batches/savepoints and 100 fresh-database Hypothesis round trips.

## Phase 3 — Domain

- [x] T3.1  `app/pool.py` : `Pool` class, in-memory + DB write-through, event bus (asyncio.Queue).
  - Immutable tuple snapshots, serialized write-through mutations, atomic check/state writes and bounded queue fanout. Tests cover 100 parallel mutations, DB failures, subscriber lifecycle and payloads. Pool owns its DB connection exclusively.
- [x] T3.2  `app/pool.py` : scoring formula from `SPEC.md §4`. Tests.
  - Exact ratio/latency/freshness formula, selection-time decay, rounded median of the last 20 measurements, persisted-check history restored on load, and dead-after-quarantine-retest boundary.
- [x] T3.3  `app/rotator.py` : four strategies (`round_robin`, `random`, `weighted`, `sticky`). Interface `pick(pool) -> Proxy | None`.
  - Active snapshots only, fail-closed empty selection, injectable RNG/clock, bounded TTLCache sticky mappings and removed-proxy invalidation.
- [x] T3.4  `app/burn_detector.py` : classify outcome. Tests for every signal in `SPEC.md §6`.
  - Pure status/body/timeout/connection/latency classification with safe fixed reasons; all signals and strict body-size, content-type and timing boundaries tested. Gateway retry integration remains T5.2.
- [x] T3.5  `app/health_checker.py` : probe one proxy; probe all with semaphore; background loop with interval. Tests with fake upstream.
  - Real local HTTP, authenticated SOCKS5 (remote DNS), and verified TLS-over-CONNECT tests; 50-proxy batch, bounded concurrency, import scheduling, quarantine retests and cancellation. Updated after review: per-probe timeouts exclude queue waits; responsive 50-proxy batches meet the target, stalled batches take longer without falsely penalizing untested proxies. Startup wiring remains T4.1.

## Phase 4 — API + UI

- [x] T4.1  `app/main.py` : FastAPI app, lifespan, mount routers.
  - Validated startup settings, off-thread key derivation, credential preflight, migrated DB, pool/rotator/checker lifecycle and readiness. Empty API routers mounted for T4.2–T4.4. `python -m app.main` maps configuration/crypto startup failures to EX_CONFIG=78; launcher scripts remain T6.1/T6.2. Local HTTP and real SQLite lifecycle tests.
- [x] T4.2  `app/routes/proxies.py` : REST per `docs/API.md`.
  - Atomic imports (any line error rejects the batch per latest user instruction), first-wins identity dedup, filtered pages/details, queued and individual checks, dead-state soft delete. Sanitized validation errors; real DB rollback/concurrency and 60-check retention tests. Fixed fake aiohttp handler leaks; test teardown rejects leaked tasks and warnings.
- [x] T4.3  `app/routes/pool.py` : stats + control endpoints.
  - Snapshot-only aggregate stats, validated rotation changes with pool_stats events, atomic dead-only purge preserving alive/quarantined records.
- [x] T4.4  `app/routes/gateway.py` : gateway status (strategy switch is T4.3).
  - Configured addresses from validated settings; bound=null and zero counters until Phase 5 replaces the runtime snapshot. TestClient verification; AnyIO pinned for compatibility with the existing Starlette version.
- [x] T4.5  WebSocket `/ws/events` broadcasts pool events.
  - Public event mapping, initial/change/2-second stats, empty gateway hook, per-client 100-item drop-oldest queues and explicit cancellation/unsubscribe on disconnect. Core checker subscribers retain overflow/resync behavior.
- [x] T4.6  Dashboard HTML (Jinja2 + HTMX) : table of proxies, forms, live updates via WS.
  - French light responsive dashboard, local HTMX 2.0.11 with license, JSON/HTML content negotiation, server-rendered OOB rows plus periodic resync, reconnect/error/loading/empty states and preserved draft/focus. TestClient coverage and Chromium desktop/mobile interaction checks; Phase 5 intentionally untouched.

## Phase 5 — Local gateways

- [x] T5.1  `app/gateway_http.py` : HTTP forward proxy + CONNECT tunnel.
  - Asyncio TCP listener with manual request-line + header parsing. CONNECT opens a raw tunnel through the chosen upstream (HTTP CONNECT or SOCKS5 via aiohttp_socks). Non-CONNECT requests forward through aiohttp with chunked / Content-Length framing preserved. Hop-by-hop headers stripped (RFC 7230) including `Connection: foo` extensions. `Via: 1.1 proxy-rotator` added. `pool.mark_success` / `mark_fail` reported per request. Wired into FastAPI lifespan; `GatewayStatus` now live (`bound`, `active_conns`, `requests_total`). Config port `0` opts into an ephemeral OS-assigned port for tests/CI. T5.2 adds burn-on-response + retry; T5.3 adds SOCKS5 inbound.
- [x] T5.2  Burn-on-response hook + automatic retry on next proxy (≤ `MAX_RETRY_PER_REQUEST`).
  - `BurnDetector` wired into `HttpGateway`. Status-based signals (401, 403, 407, 429, 451, 503) trigger retry *before* any bytes are written to the client. Small inspectable bodies (`text/*` or `application/json`, Content-Length ≤ 128 KiB) are peeked for body markers and retried if burned. Transport errors (timeout, connection refused/reset) also retry. CONNECT retries on upstream-connect failures only (tunnel is non-replayable once bytes bridge). Idempotency: GET/HEAD/DELETE/OPTIONS and small POST/PUT (Content-Length ≤ 1 MiB) are buffered and retryable; larger bodies and chunked uploads make a single attempt. Settings add `max_retry_per_request` (default 2) and `burn_inspect_body` (default true). 4 new tests cover retry success/exhaustion/status-pass-through.
  - Correction (post-audit): only 1 of those 4 tests (`test_retry_exhausted_returns_502`) actually exercised retry; `test_retry_on_upstream_connect_error_then_success` and `test_retry_on_burn_status_then_success` were vacuous (they asserted a plain 200 and would pass with retry deleted). Both were rewritten to force a real first-attempt failure, and `test_no_retry_on_2xx` now counts upstream attempts. See “Post-audit corrections” below.
- [x] T5.3  `app/gateway_socks.py` : SOCKS5 no-auth CONNECT server with DNS passthrough.
  - RFC 1928 handshake (version 5, NO AUTH method). CONNECT only; BIND and UDP ASSOCIATE return REP=0x07. ATYP IPv4, IPv6 and domain all supported — the domain name is passed through to the upstream, so DNS resolves on the proxy side (no local DNS leak). Retry on upstream-connect errors up to max_retry_per_request. Wired into FastAPI lifespan beside the HTTP gateway. 7 new tests cover status, HTTP-upstream tunnel, domain pass-through, pool-empty, unsupported method, unsupported command, success counter.
- [x] T5.4  `scripts/smoke_gateway.sh`: three proxied IP requests; at least two distinct exits (updated acceptance per user).
  - Bounded readiness/pool waits, validated IP-only output, explicit proxy use and owned process-group cleanup. Offline success/failure/cleanup tests pass. Live proof SKIPPED: no .env or loaded upstream proxies in this workspace; requires live upstream proxies.

## Phase 6 — Packaging

- [x] T6.1  `scripts/start.sh` : auto-create venv if missing, install deps, start.
  - Early .env check (78), dependency-free --help, Python version validation, module-based pip, timestamp refresh only on success, data directory and exec of app.main/Uvicorn with validated .env bindings. Isolated bootstrap/failure/path-with-spaces tests pass. Gate prerequisite: fake gateway upstreams now always close incoming writers; per-test garbage collection surfaces resource leaks at teardown.
- [x] T6.2  `scripts/start.bat` : same for Windows.
  - Checks Python, .env (78), install failures and requirement timestamps; creates data. findstr pipes only bindings to a Python parser, never shell-evaluates values. Cross-platform parser tests pass; native batch test included. Native execution SKIPPED locally (no Windows), deferred to T6.3 matrix.
- [x] T6.3  GitHub Actions matrix (ubuntu-latest, macos-latest, windows-latest) : `pip install`, `pytest`, `ruff`, `mypy`.
  - Python 3.11, fail-fast matrix, explicit pytest/Ruff/mypy steps; no live gateway smoke. Hosted execution SKIPPED: local-only repository, no GitHub remote. Windows validation remains pending its first hosted run.
- [x] T6.4  README Dashboard ASCII sketch added: topbar, summary, table, import/rotation/gateway panels and bound addresses. Feature list corrected to match implemented MVP.

## Phase 7 — Done gate

- [x] T7.1  All Phase 0–6 implementation boxes checked; external validation exceptions explicitly recorded as SKIPPED.
- [x] T7.2  Local CHANGELOG.md summarizes the 23 post-bootstrap development commits and packaging work (no PR or remote per user).
- [x] T7.3  Local final report: 264 tests pass, 1 native Windows test skipped; Ruff/mypy green.
  - Three-rotation proof SKIPPED: requires live upstream proxies; smoke exits 1 because .env is absent. Hosted matrix SKIPPED until a remote exists. Final response reports these limits instead of claiming live proof.
  - Known debt outside this packaging scope: dashboard Gateway status label remains static/configured; actual bound state is available via API. Recorded in CHANGELOG.md for a future issue when GitHub exists.

---

## v2 — Deferred (NOT in v1, listed for context)

- Electron shell wrapping the FastAPI backend.
- Multi-pool regional routing.
- Dashboard graphs (requests/min, success % over 1 h).
- Subscription URL auto-poll + diff.
- Sticky session per client cookie.
- Basic auth on the admin UI.
- Browser-config helper (Firefox autoconfig, SwitchyOmega preset).

## User-requested follow-up

- [x] Import valid proxies while skipping malformed or unauthenticated lines; preserve error line numbers, deduplication and transactional database writes. Supersedes T4.2 whole-batch rejection per latest user instruction.

- [x] Review corrections: single active manual test batch with progress; per-probe timeout after queue wait; safe CONNECT 407/TLS diagnostics; verified TLS for HTTPS upstream tunnels from both HTTP and SOCKS5 listeners; cleanup on partial startup failure; isolated gateway ports in application tests.

- [x] User-requested dashboard clarity: general batch progress bar and outcome counts, persistent live sorting, Rotation/Passerelles moved above the table, live gateway activity/counters.

- [x] User-requested live usage: active and recent HTTP/SOCKS5 connections, last verified exit IP and changes, country lookup integration (opt-in), health score and elapsed connection time; country/minimum-score/strict-pin selection for new connections.
- [x] User-requested gateway tests: separate HTTP and SOCKS5 buttons, bounded end-to-end IP request through each actual listener, no direct fallback, duplicate-test guard and persistent feedback during live refresh.
- [x] User-requested timed rotation: “Toutes les X minutes”, interval 1–1440 minutes, stable selection until deadline, next eligible proxy on the next request, immediate replacement if unavailable; existing tunnels continue.
- [x] User-requested Finder restart: start.command stops this project's existing Python/Uvicorn app.main processes (command + working-directory verification), SIGTERM with bounded wait then SIGKILL, before bootstrap. Other projects remain untouched; help does not stop servers.
- [x] User-requested compact proxy table: full available width, narrower cells and wrapping metadata, visible action column; stacked labeled rows on small screens without horizontal scrolling.
- [x] User-requested long-page shortcuts: prominent top “Importer une liste”, fixed Import/Back-to-top links, native anchors and immediate scrolling.

## Post-audit corrections (Palier 1)

Findings and evidence are recorded in `AUDIT.md`. Each fix below has a regression
test that was verified to fail against the previous code (mutation check).

- [x] A1/A2 — no retry once a response has committed to the client. `_AttemptState`
  records the first byte written; a transport failure after that point logs
  `gateway_http_stream_interrupted` and returns instead of appending a second
  HTTP response. The whole-transfer `wait_for` was replaced by
  `ClientTimeout(sock_connect, sock_read)` so a slow but progressing transfer is
  no longer cut at `GATEWAY_TIMEOUT_S` (RFC 9112 §9.2). Regression tests:
  `test_partial_upstream_body_is_never_retried`,
  `test_slow_but_progressing_response_is_not_cut_by_the_timeout`.
- [x] A3 — request framing validated per RFC 9112: `Content-Length` together with
  `Transfer-Encoding`, a non-final `chunked` coding, and non-integer or
  conflicting `Content-Length` values are rejected with 400 and the connection
  is closed. Duplicate *identical* lengths are accepted and collapsed to one
  field, as §6.3(5) requires. Regression tests:
  `test_rejects_content_length_with_transfer_encoding`,
  `test_rejects_conflicting_content_length_headers`,
  `test_rejects_transfer_encoding_without_final_chunked`,
  `test_accepts_duplicate_identical_content_length`.
- [x] A4 — `chunk-size` must match `1*HEXDIG` (RFC 9112 §7.1): signed or
  over-long numerals are refused, and the trailer section is bounded. The
  previous `int(size_text, 16)` accepted `-1`, which reached
  `StreamReader.read(-1)` and blocked while buffering without limit. Regression
  test: `test_rejects_negative_chunk_size` (uses a silent upstream so the only
  possible 400 is the gateway's own).
- [x] A8 — non-idempotent requests are never replayed (RFC 9110 §9.2.2,
  RFC 9112 §9.3.1): POST/PATCH take a single attempt, and a burned response is
  forwarded to the client unchanged (the client sees the origin's real status)
  while the burn is still recorded against the proxy. Regression test:
  `test_non_idempotent_request_is_not_retried_on_burn`.
- [x] A9 — deleting a proxy during an open tunnel no longer raises `KeyError`
  into the 500 handler, which previously injected `HTTP/1.1 500` into an
  established TLS tunnel.
- [x] B1 — an `unknown` proxy that failed its first check is no longer stranded:
  `HealthChecker.due()` now reselects it every interval, so it reaches
  quarantine automatically instead of waiting for a manual retest. Regression
  test: `test_proxy_that_failed_its_first_check_is_still_scheduled`.
- [x] B2 — CONNECT tunnels record liveness without touching the latency history,
  so a burst of tunnels can no longer collapse `latency_ms` towards zero and
  inflate the score. Regression tests:
  `test_tunnel_success_does_not_pollute_latency`,
  `test_socks5_marks_success_on_tunnel_close`.
- [x] E1/E2 — the two vacuous retry tests were rewritten and
  `test_no_retry_on_2xx` now asserts the upstream attempt count.

Gate: `pytest -q` → 291 passed, 1 skipped, 1 failed; `ruff check app tests` and
`mypy app` clean. The single failure is `test_start_command.py`, which needs
`/bin/ps` and is blocked in the agent sandbox (see AUDIT.md §6 E7); it is
unrelated to these changes and fails identically on the unmodified tree.

## Post-audit corrections (Palier 2)

- [x] H1 — dependency advisories cleared and kept cleared. `fastapi` 0.115.0 →
  0.143.0, explicit `starlette==1.7.0` (seven advisories; the minimum fixing
  version was 1.3.1), `aiohttp` 3.10.11 → 3.14.5, `cryptography` 43.0.1 →
  50.0.2, `anyio` 4.11.0 → 4.15.1, `pytest` 8.3.3 → 9.1.1 with
  `pytest-asyncio` 1.4.0 and `hypothesis` 6.169.0, `pip-audit` 2.10.1.
  `pip-audit -r requirements.txt` went from **49 advisories in 5 packages** to
  **no known vulnerabilities**. The dev dependency `httpx` is replaced by
  `httpx2`, which the Starlette 1.x TestClient requires. A new blocking `audit`
  job in `.github/workflows/ci.yml` keeps it that way — `docs/SECURITY.md`
  promised that step while the workflow never ran it, which is how the
  advisories accumulated.
  - Code changes the bump required: `aiohttp.BasicAuth` and `proxy_auth` are
    deprecated in 3.14, so proxy credentials now travel in the proxy URL
    userinfo (yarl encodes them correctly, aiohttp derives the header);
    `app/routes/events.py` wraps its teardown `gather` in
    `suppress(asyncio.CancelledError)`, because a client disconnect cancels the
    handler mid-cleanup and Starlette 1.x surfaces that as an escaped
    `CancelledError`; the fake upstreams in the gateway tests now own their
    accepted handler tasks so none survive the loop.
- [x] C1 — exposure guards. `app/security.py` adds three ASGI middlewares:
  `HostGuard` (unknown `Host` → 400, defeats DNS rebinding), `CrossSiteGuard`
  (state-changing request a browser marks cross-site, a foreign `Origin` or
  `Origin: null` → 403, defeats CSRF against the loopback API), and
  `TokenGuard` (no-op unless `ADMIN_TOKEN` is set; then every route except
  `/healthz`, `/login` and `/static/*` needs the token as a Bearer header or
  through the cookie set by the new `/login` page).
  A non-loopback `BIND_HOST` is now **refused** (EX_CONFIG 78) unless
  `ALLOW_REMOTE_BIND=true`, a non-empty `ADMIN_TOKEN` and a non-empty
  `ADMIN_ALLOWED_HOSTS` are all present; the startup warning states that the
  gateways stay unauthenticated. This deliberately supersedes CLAUDE.md rule 3
  ("flip `BIND_HOST=0.0.0.0`" as the only step), which the audit found would
  publish an unauthenticated admin UI *and* two open proxies.
  - Verified on a real uvicorn instance, not only through ASGI tests:
    `/healthz` 200 without a token, `/api/pool/stats` 401 without and 200 with
    a Bearer token, foreign `Host` 400, cross-site POST 403, same-origin POST
    200, dashboard 200 with the login cookie. The default loopback setup without
    `ADMIN_TOKEN` is unchanged (dashboard, API and export all 200).
- [x] C4 (partial) — `docs/SECURITY.md` no longer claims controls that do not
  exist: the "enforced by lint rule" masking claim, `app.log.mask()` and
  `pytest tests/security/` are replaced with what is actually true, the
  `pip-audit` claim is now backed by a real CI job, and the residual risks
  (plaintext credential export on loopback, unauthenticated gateways, token in
  clear over remote HTTP, 0644 database files) are written down.

Gate: `pytest -q` → 311 passed, 1 skipped, 1 failed; `ruff check app tests` and
`mypy app` clean; `pip-audit -r requirements.txt` → no known vulnerabilities.
The single failure remains the sandbox-blocked `test_start_command.py`.

### Complément d'entrée (même journée)

- [x] A5 — `Expect: 100-continue` est désormais honoré par le gateway lui-même
  avant de lire le corps (RFC 9110 §10.1.1) et l'en-tête n'est pas transmis :
  un client conforme ne peut plus s'interbloquer face à un gateway qui attend le
  corps. Test : `test_expect_continue_is_answered_locally`.
- [x] A6 — la lecture du corps client est bornée par un budget d'inactivité. Un
  client qui annonce un corps puis se tait reçoit un 400 au lieu de retenir la
  connexion indéfiniment, et la panne est classée comme erreur *client* : elle
  ne marque plus le proxy en échec. Tests :
  `test_stalled_request_body_is_a_client_error`,
  `test_stalled_chunked_upload_does_not_penalise_the_proxy`.
- [x] A10 — les identifiants SOCKS5 amont sont passés à `python_socks` en
  arguments nommés au lieu d'être interpolés dans une URL `socks5://`. Cette URL
  était analysée avec `unquote` : `p%41ss` devenait silencieusement `pAss` et
  `/ ? # [` levaient un `ValueError`. Passer les valeurs directement supprime
  l'étape d'encodage au lieu de tenter de l'échapper, et retire au passage
  l'usage de `aiohttp_socks.open_connection`, déprécié (il avertissait à chaque
  tunnel et levait sous `filterwarnings=error`). `python-socks` est maintenant
  déclaré explicitement. Nouveau module `app/socks_upstream.py`, test
  `test_connect_through_socks5_upstream_sends_exact_credentials` (avec un vrai
  faux serveur SOCKS5 qui refuse le tunnel si les identifiants diffèrent).
- [x] A11 — `LimitOverrunError` sur une ligne d'en-tête trop longue répond 400
  au lieu de s'échapper en 500. Test :
  `test_overlong_header_line_is_a_client_error`.

Les cinq tests ci-dessus ont été vérifiés en mutation : chacun échoue contre le
code d'origine.

Gate: `pytest -q` → 316 passed, 1 skipped, 1 failed; Ruff et mypy propres ;
`pip-audit` sans vulnérabilité connue.

### Palier 3 (partiel) — robustesse et observabilité

- [x] A7 — plafond de connexions par passerelle (`GATEWAY_MAX_CONNECTIONS`,
  défaut 256, HTTP et SOCKS5 comptés séparément). Au-delà, le gateway HTTP
  répond `503 Gateway Busy` et le gateway SOCKS5 ferme la connexion : refuser
  plutôt qu'accumuler des tâches, deux sockets et jusqu'à 1 Mio de corps
  bufferisé par connexion. Tests :
  `test_http_gateway_refuses_beyond_max_connections`,
  `test_socks5_gateway_refuses_beyond_max_connections`.
- [x] B3 — `Pool.mark_fail` ne jette plus le motif : il est journalisé
  (`pool_mark_fail proxy_id=… reason=…`), ce qui rend les brûlures pilotées par
  la passerelle diagnostiquables. La persistance du motif demanderait un
  changement de schéma et reste volontairement différée. Test :
  `test_mark_fail_records_the_reason`.
- [x] B7 — les signaux de brûlure SPEC §6 sont désormais réellement branchés :
  - les échecs de transport sont classés par le détecteur, donc un proxy
    injoignable est enregistré comme `Connection failure` et non plus comme le
    générique `upstream_error` ; un délai d'attente devient `Timeout` ;
  - le pic de latence est appliqué là où la mesure est comparable à la médiane,
    c'est-à-dire dans le health checker, qui marque alors la sonde en échec avec
    le motif `Latency spike` ;
  - `classify` ne confond plus une réponse complète et lente avec un timeout :
    la règle fondée sur la durée ne s'applique que si aucune réponse n'est
    arrivée, ce qui est la contrepartie du passage au budget d'inactivité (A2/A6).
  Tests : `test_connection_failure_reason_reaches_the_pool`,
  `test_latency_spike_fails_the_probe`,
  `test_a_completed_response_is_never_reported_as_a_timeout`.
- Contrôle de mutation : six des sept nouveaux tests **échouent** contre le code
  d'origine. Le septième, `test_a_steady_proxy_is_never_a_latency_spike`, est un
  **garde anti-faux-positif** : il passe forcément contre l'ancien code, qui ne
  détectait aucun pic. Il est listé comme tel et ne sert pas de preuve.

Gate: `pytest -q` → 323 passed, 1 skipped, 1 failed; Ruff et mypy propres ;
`pip-audit` sans vulnérabilité connue.

### F1 — preuve de rotation, toujours bloquée

`scripts/smoke_gateway.sh` n'a **pas** pu être exécuté : la base ne contient
aucun proxy vivant (0 `alive`, 74 `unknown`, 18 `quarantine`, 8 `dead`) et la
dernière réussite date du 2026-10-09T08:27. Aucune preuve de rotation n'existe
donc toujours, et rien dans ces correctifs ne peut en produire une sans
upstreams fonctionnels. La condition reste : deux proxies vivants avec deux IP
de sortie distinctes.

## T7 — Dichvusocks provider integration (addendum, 2026-10-12)

Previously undocumented (audit H4.4). The provider ships three units:

- `app/providers/dichvusocks.py` — thin async client over
  `https://dichvusocks.net/api/v1` (`user_info`, `countries`, `list_socks`,
  `view`). Only `view()` debits credit, and only when the operator
  explicitly imports.
- `app/provider_store.py` — Fernet-encrypted token in a `provider_tokens`
  SQLite row (migration 002).
- `app/routes/providers.py` — `/api/providers/dichvusocks/{status,user-info,
  countries,preview,import,token}`.

The feature is opt-in: no HTTP call is made without a token, and
`DichvusocksClient.enabled` gates each API method. Covered by
`tests/test_provider_store.py`, `tests/test_dichvusocks_client.py` and
`tests/test_providers_routes.py` (29 tests driven by a local aiohttp
upstream — no real credit is spent).

## F1 — Rotation proof (2026-10-12)

Captured against a running pool of 42 alive proxies: 5 HTTP requests
through `127.0.0.1:8888` returned 5 distinct exit IPs
(`45.154.138.{115,135,157,158,130}`); 3 SOCKS5 requests through
`127.0.0.1:1080` returned 3 more (`45.154.138.{118,156,110}`). Eight
distinct exit IPs across eight requests on both gateways.

Still open from the audit: B8, E4–E8, F2 (doc), F3–F9, G1–G4, H3 (smoke
script auto-attach).

## T8 — Sélection par ville (demande utilisateur, 2026-10-14)

- [x] Filtrer les cinq stratégies par pays/région/ville, sans repli si la ville
  est inconnue ou ne correspond pas ; suivre les changements d'IP de sortie.
- [x] Migration 003 : cache ville/région ; conservation des métadonnées
  Dichvusocks et lecture des schémas ipapi/ip-api.
- [x] Champs région/ville dans les réglages et l'aperçu/import Dichvusocks,
  suggestions des villes connues, validation et aide intégrées.
- [x] Tests ciblés : 39 passed (rotation, API ASGI sans listeners, import avec
  fournisseur simulé, cache, migration, DB et observations d'IP). Ruff, mypy,
  syntaxe JavaScript et diff --check passent. Import asyncio manquant corrigé
  dans le test préexistant de purge du pool ; modifications locales préservées.
- [ ] Validation complète hors sandbox : tentative de suite complète, 222
  passed, 2 skipped, 28 failed, 124 errors ; ports locaux interdits par le
  sandbox et test préexistant du lanceur également en échec. Aucun import réel
  payant ni preuve contre l'API Dichvusocks distante dans cette session.

### T8.1 — Ville dans le tableau principal

- [x] Afficher ville et pays sur chaque ligne, y compris dans les fragments live.
- [x] Ajouter « Ville : A à Z » au menu de tri ; ignorer casse/accents,
  placer les villes inconnues en dernier et départager par ID.
- [x] Vérifier page initiale, snapshot après changement de ville et fragment
  individuel via ASGI sans listeners : 34 tests ciblés passent ; Ruff/mypy verts.

## T9 — Diagnostic visible et filtres du tableau (2026-10-14)

- [x] Afficher le motif du dernier test en échec sous l’état, en français,
  avec une action utile et sans exposer les messages bruts. Restaurer le dernier
  test depuis SQLite au démarrage ; retirer l’erreur après une sonde réussie.
- [x] Filtres d’affichage ville (recherche sans casse/accents), pays (code) et
  état, combinables avec le tri. Inclure explicitement les hors pool sur demande.
- [x] Conserver les filtres dans l’URL et les snapshots live ; compteur visible,
  suggestions actualisées et réinitialisation. Aucun effet sur la rotation.
- [x] Harmoniser les libellés « Utiliser en priorité » et « Retirer la priorité ».
- [x] Tests ASGI sans sockets, persistance, erreurs connues/inconnues, filtrage,
  fragments et échappement HTML ; vérifications Ruff/mypy/JavaScript.
- [ ] Contrôle visuel en navigateur : Chromium interdit par le sandbox
  (`bootstrap_check_in: Permission denied`). Revue source et détecteur sans anomalie.

### T9.1 — Priorité dans Rotation

- [x] Déplacer « Utiliser uniquement les proxies prioritaires » sous le mode
  de rotation et l’appliquer avec « Appliquer la stratégie ».
- [x] Préserver les filtres géographiques au changement de priorité, et la
  priorité au changement de filtres. API existante compatible lorsque le champ
  est omis. Test de régression ASGI : 37 tests ciblés passent ; Ruff/mypy/JS verts.

### T9.2 — Rotation expliquée et priorité simplifiée

- [x] Cocher la restriction aux proxies prioritaires sélectionne automatiquement
  « À tour de rôle » dans le formulaire ; l’application reste explicite au clic.
- [x] Chaque mode possède une explication visible, rendue côté serveur puis mise
  à jour à la sélection (zone aria-live). Décocher conserve le mode choisi.
- [x] Test comportemental JS via `node --test tests/rotation_controls.test.cjs` :
  initialisation, case et cinq modes validés ; 37 tests Python ciblés passent.

### T9.3 — Verrouillage du mode prioritaire

- [x] Case cochée : menu désactivé sur « À tour de rôle », y compris au chargement.
  Case décochée : menu réactivé. Explication visible mise à jour.
- [x] Envoyer explicitement le mode même lorsque le select est désactivé ;
  imposer aussi la cohérence côté API. Tests JS de verrouillage et soumission,
  régression API et 37 tests Python ciblés passent ; Ruff/mypy verts.

## T10 — Diagnostic du panneau Dichvusocks (2026-10-14)

- [x] Cause de la capture : aucun token configuré, mais `display:flex` du bloc
  compte écrasait le comportement HTML `hidden`. Masquage explicite de tous les
  éléments hidden du panneau et état « Aucun token enregistré ».
- [x] Corriger `schedule is not defined` après import (fonction dans une autre
  closure), qui transformait un succès en erreur ; publier `pool-refresh`.
- [x] Remplacer la liste des pays au rechargement sans doublons ; borne entière
  1–50 avant import ; ne pas afficher `undefined` pour un import vide.
- [x] Chargement compte/pays limité à 12 s, messages exploitables, bouton de
  relance, protection contre les réponses d’une ancienne configuration.
- [x] Tests : 8 JS (7 panneau + 1 rotation) et 32 Python passent ; cycle token
  réel sur SQLite/ASGI sans socket. Ruff/mypy verts, détecteur UI sans anomalie.
  Aucun appel réel payant ; validation contre le fournisseur distant non faite.

## T11 — Authentification visible (2026-10-14)

- [x] Indiquer « Auth : identifiant / mot de passe » sur chaque ligne du tableau,
  y compris les fragments live. Le parseur exige actuellement des identifiants ;
  les entrées sans identifiants ne sont pas prises en charge. Une éventuelle
  liste blanche IP chez le fournisseur ne peut pas être déduite de ces données.

### T11.1 — Lots mixtes (demande utilisateur)

- [x] Accepter les entrées sans identifiants dans le parseur et le modèle,
  conserver la déduplication par protocole/hôte/port/identifiant et l’export.
- [x] Omettre l’authentification HTTP/SOCKS lors des sondes et connexions
  pour ces entrées ; conserver les mots de passe chiffrés au repos.
- [x] Remplacer le libellé uniforme T11 par « Avec identifiants » /
  « Sans identifiants » ; documenter les formats et la limite de détection IP.
- [x] Validation : suite complète 396 réussis, 2 ignorés ; seul échec = ancien
  contrat rejetant les entrées sans identifiants. Contrat mis à jour puis test
  ciblé réussi (397 tests validés au total). Ruff, mypy et diff --check passent.
  Aucun proxy réel du lot utilisateur importé ni contacté.

### T11.2 — Compteurs d’authentification

- [x] Rapport de chaque import manuel/fichier/fournisseur : nombre ajouté
  avec et sans identifiants, doublons exclus. Champs API with_auth/without_auth.
- [x] Deux compteurs dans le résumé global, actualisés dans les snapshots live
  et calculés depuis le pool (tous les états, indépendants des filtres du tableau).
- [x] Régression import mixte, doublons, retour HTML, API fournisseur et résumé.
- [x] Validation : 398 tests Python réussis, 2 ignorés ; 8 tests JavaScript
  réussis. Ruff, mypy, diff --check et détecteur UI passent.

### T11.3 — Retrait ciblé, import vérifié et compteurs actifs

- [x] Bouton dashboard « Retirer les sans identifiants », annulation des sondes,
  suppression persistante et rafraîchissement des totaux ; identifiants conservés.
- [x] Tester les nouveaux candidats avant transaction d’import : échecs non
  enregistrés, succès sauvegardés avec résultat et IP de sortie, doublons exclus.
- [x] Appliquer aux imports manuels/fichiers et fournisseur, rapport d’échecs,
  message d’attente ; aucun appel au lot réel utilisateur pendant les tests.
- [x] Deux compteurs actifs avec/sans identifiants, basés sur l’état alive,
  indépendants des totaux et des filtres du tableau.
- [x] Validation T11.3 : suite complète 401 réussis, 2 ignorés, 11 sous-tests ;
  8 tests JS, Ruff et mypy verts. Serveur local redémarré pour charger les
  nouveaux champs ; API vérifiée avec 71 actifs avec identifiants et 0 sans.

### T11.4 — Export par pays et résumé compact

- [x] Export multi-pays à partir des seuls actifs, effectifs et pays inconnu
  si nécessaire ; aucun pays sélectionné = tous. Validation serveur des codes.
- [x] Préserver les choix lors des mises à jour ; un pays disparu reste
  sélectionné avec aucun disponible, sans élargissement silencieux de l’export.
- [x] Résumé sur deux lignes (5 indicateurs puis 4 compteurs), zéro explicite.
- [x] Test API exports simple/multiple/tous/inconnu/vide/invalide ; revue Chromium
  du résumé et de la conservation/réinitialisation des cases après quarantaine.

### T11.5 — Nom du fichier exporté

- [x] Inclure les codes pays sélectionnés, triés et dédupliqués, dans le nom
  du fichier ; tous-pays sans filtre et pays-inconnu pour les IP non localisées.

## T12 — Navigateurs (demande utilisateur, 2026-10-15)

- [x] Détecter les navigateurs installés et lancer des profils isolés.
- [x] Proposer Chromium, Firefox et WebKit dédiés, installation à la demande.
- [x] Sessions avec rotation du pool ou proxy fixe ; passerelle locale dédiée,
  refus si proxy indisponible, fermeture et nettoyage contrôlés.
- [x] Page Navigateurs : catalogue, installation, lancement et sessions ouvertes.
- [x] Tests du routage, du cycle de vie et des contrôles UI ; documentation.
- Validation : 409 tests Python réussis, 2 ignorés, 11 sous-tests ; Ruff/mypy
  verts. Chromium, Chrome et Firefox réels lancés sur une page locale via un
  proxy authentifié, puis fermés. Le faux serveur refuse les CONNECT de fond :
  leur pénalité est neutralisée uniquement dans ce smoke de navigation HTTP.
  Refus de repli testé séparément sur une vraie passerelle locale.
- UI vérifiée dans Chromium : trois moteurs, choix multi-proxy, desktop/mobile
  sans débordement. Firefox dédié/WebKit et Windows non exécutés ; téléchargement
  laissé à la demande. Contrat de nom d’export UTC adapté aux pays déjà ajoutés.

## T13 — Fiabilisation Windows et sessions (accord utilisateur, 2026-10-15)

- [x] Reconnaître les erreurs Winsock et stabiliser le test de timeout.
- [x] Respecter les réglages de passerelle et les filtres en mode rotation.
- [x] Vérifier fermeture et suppression des profils, signaler/reprendre les échecs.
- [x] Encadrer Python Windows et améliorer les diagnostics des moteurs.
- [x] Tests Windows des trois moteurs, HTTP/HTTPS et absence de repli direct.
- [x] Distribution Windows propre avec instructions et sans données utilisateur.

- Reprise du 16 octobre : nettoyage des lancements multiples poursuivi même si
  une fermeture échoue ; erreurs de téléchargement affichées et état débloqué.
  Whoer conservé comme page de départ. Profils exclus de Git et de la distribution.
- Validation macOS : 425 tests Python réussis, 2 ignorés, 11 sous-tests ; inclut
  les six scénarios réels Chromium/Firefox/WebKit avec/sans authentification,
  HTTP/HTTPS, rotation/proxy fixe, refus de repli direct, fermeture et nettoyage.
  8 tests JS réussis ; Ruff/mypy et diff --check verts. L’ancien Node global
  ne démarre pas (bibliothèque ICU manquante), Node Homebrew node@22 utilisé.
- Windows : job dédié ajouté à `.github/workflows/ci.yml` ; exécution Windows
  encore à confirmer. Guide `docs/WINDOWS.md`, construction reproductible via
  `scripts/build_windows.py`. Archive locale `dist/proxy-rotator-windows.zip`.
- Limite conservée : les profils résiduels après fermeture ratée sont visibles
  et réessayables pendant la session, mais ne sont pas repris après redémarrage.

- Première CI Windows du 16 octobre : six scénarios navigateurs réussis ;
  suite générale 409 réussis, trois échecs et une erreur de nettoyage liée.
  Corrections : le faux téléchargeur laisse maintenant passer taskkill ;
  WSAETIMEDOUT reste un Timeout sur tous les OS ; le détecteur consulte aussi
  os_error.winerror pour ERROR_CONNECTION_REFUSED (1225) de Proactor/aiohttp.
  Régression locale ajoutée pour cette enveloppe d’exception.

### T13 — Validation finale et point de reprise (16 octobre 2026)

- **Terminé et sauvegardé** sur `fix/windows-browser-sessions-20261016`.
  Code validé : `58f7cd6`. Branche envoyée au dépôt privé ; main non fusionnée.
- [CI finale](https://github.com/nik121212T/proxy-rotator/actions/runs/38912519830) :
  Windows/macOS/Linux, Ruff, mypy et audit des dépendances réussis. Six scénarios
  réels des trois moteurs Windows réussis. Dernière régression macOS générale :
  420 tests réussis, 8 ignorés (dont 6 navigateurs déjà exécutés séparément).
- Application locale redémarrée et contrôlée : `/healthz`, `/navigateurs` et
  `/api/browsers` répondent ; trois moteurs disponibles, aucune session ouverte.
  Adresse : http://127.0.0.1:8081.
- Archive finale : `dist/proxy-rotator-windows.zip`, reconstruite depuis les
  sources corrigées ; guide `docs/WINDOWS.md`, aucun secret/base/profil inclus.
- Suite éventuelle : essai utilisateur manuel Windows 11, puis intégration de
  la branche dans main. La CI ne certifie pas les fuites DNS/WebRTC ni toutes
  les configurations de navigateurs installés. Aucun nouveau chantier lancé.

## T14 — Liste des IP enrichie dans le dashboard (16 octobre 2026)

- [x] Localisation dédiée (pays, ville, région), fiabilité regroupée et âge du
  dernier test ; conserver actions, filtres et mises à jour en direct.
- [x] Vérifier desktop/mobile, données absentes et échappement des données.

- Tableau conservé à sept colonnes ; suppression des encadrés répétitifs des
  métadonnées, localisation sur trois lignes, score/réussite regroupés.
- Vérification Chromium sur données fictives à 1440 px et 390 px : aucun
  débordement horizontal. Les tests couvrent pages, snapshots et lignes OOB,
  tri par ville, région échappée et absence de mot de passe dans le rendu.
- Application rechargée sur http://127.0.0.1:8081 ; page et snapshot contrôlés.
  Aucun champ blacklist/hostname fournisseur ni durée en ligne inventés.
- Validation finale T14 : 421 tests Python réussis, 8 ignorés, 11 sous-tests ;
  Ruff/mypy et diff --check verts. Archive Windows reconstruite.

## T15 — Taille et suppression des moteurs dédiés (16 octobre 2026)

- [x] Afficher la taille installée de chaque moteur et proposer sa suppression.
- [x] Bloquer la suppression pendant une session ou une installation et permettre
  de retélécharger ; préserver les autres moteurs et les navigateurs personnels.

- Validation T15 : 424 tests réussis, 8 ignorés ; Ruff/mypy/JS verts.
  Parcours Chromium avec API simulée : confirmation, refus en utilisation,
  suppression et retour du téléchargement ; mobile sans débordement.
- Taille des fichiers des révisions courantes (Chromium inclut son headless shell),
  calcul mise en cache 30 s, invalidation après installation/suppression.
  Dossiers inconnus et liens symboliques refusés ; ffmpeg et autres moteurs
  préservés. Cache Playwright partagé signalé dans la confirmation.

## T16 — Profils BitBrowser réservés au gestionnaire (16 octobre 2026)

- [x] Connexion à l’API locale configurable dans Navigateurs ; clé chiffrée.
- [x] Création et réutilisation d’un profil rotation et d’un profil par proxy fixe.
- [x] Passerelle privée, fermeture et suivi ; ne modifier que les profils créés ici.
- [x] Tests avec API locale simulée et documentation.
- [ ] Essai réel : BitBrowser absent sur le Mac ; installation et connexion du compte nécessaires.

Validation T16 : 430 tests réussis, 8 ignorés ; Ruff, mypy et syntaxe JS verts.
Parcours Chrome avec API simulée : connexion, sélection, effacement du champ clé ;
mobile sans débordement. Profils réutilisés, propriété contrôlée et passerelle
arrêtée même si la fermeture distante échoue.

## T17 — Mesure et optimisation des grands pools (17 octobre 2026)

- [x] Benchmark reproductible et isolé : démarrage, dashboard, ligne, sélection,
  mémoire temporaire de rendu et débit HTTP via un faux upstream local.
- [x] Corriger les coûts confirmés par les mesures, avec tests de régression.
- [x] Documenter avant/après et limites ; validation générale et archive.

Résultats et protocole : `docs/PERFORMANCE.md`. Sur 10 000 proxies synthétiques,
chargement 1 367 → 340 ms, ligne 95,66 → 0,67 ms, sélection 43,91 → 16,82 ms ;
débit HTTP local 16,3 → 38,7 req/s. Le tableau complet reste lourd ; pagination
côté serveur identifiée comme prochaine priorité, hors de cette première passe.

Validation T17 : 434 tests réussis, 8 ignorés, 11 sous-tests ; Ruff et mypy verts.
Archive Windows reconstruite, sans base ni profils utilisateur.

## T18 — Progression réelle de l’import, au niveau du formulaire

- [x] Compteurs serveur isolés par import : analyse, tests, enregistrement, résultat.
- [x] Barre de progression et retour local, sans déplacement de page ; erreurs et doubles clics.
- [x] Tests backend et parcours navigateur simulé ; préserver tout import utilisateur en cours.

- Diagnostic du lot signalé : 125 lignes valides, toutes préfixées HTTPS ;
  3 échantillons expirent en HTTPS à 8 s, TCP accessible et HTTP sans
  identifiants répond 407. Compatibilité HTTP probable ; aucune conversion
  ni transmission d’identifiants en HTTP faite pendant le diagnostic.
- Choix explicite du protocole ajouté : préfixes préservés par défaut,
  remplacement seulement sur sélection utilisateur, déduplication après choix.
- Suivi en mémoire : identifiant UUID par import, compteurs sans secrets,
  maximum 64 suivis et expiration des résultats après 10 minutes.
- Validation : 438 tests réussis, 8 ignorés ; Ruff/mypy/JS verts. Parcours
  Chrome simulé : compteurs, bouton bloqué, protocole, résultat local, défilement
  stable et mobile sans débordement.

### Vérification utilisateur après T18

- Pool réel observé : 12 proxies disponibles. Dashboard médian 3,89 ms,
  snapshot 2,82 ms, ligne 1,02 ms. Mesures détaillées dans `docs/PERFORMANCE.md`.
- 3/3 requêtes HTTPS via HTTP et 3/3 via SOCKS5 réussies, sur une copie SQLite
  en mémoire obtenue par lecture seule ; aucune mutation du pool réel.
- Nouveau serveur d’import confirmé actif par son schéma OpenAPI. Aucun
  redémarrage supplémentaire effectué pendant les essais utilisateur.

## T19 — Lanceur de push sur la branche de travail

- [x] Envoyer la branche courante au lieu d’imposer main ; afficher son nom.
- [x] Refuser HEAD détachée. Tests avec faux Git : main, branche de travail,
  absence de branche ; aucun accès au trousseau ou au réseau dans ces tests.

Validation T19 : 441 tests réussis, 8 ignorés ; Ruff, mypy et bash -n verts.

## T20 — Fiabiliser la mise à jour GitHub

- [x] Authentification privée, erreurs explicites et release stable exacte.
- [x] Installation Python portable ; dépendances préparées dans un environnement séparé.
- [x] Arrêt de l’arbre de processus, restauration du code sur erreur et tests isolés.
- [x] Documentation, validation et préparation de la release v0.1.2.

Validation T20 : 472 tests réussis, 8 ignorés, 11 sous-tests ; Ruff, mypy,
JavaScript et syntaxe Bash verts. Installation réelle du tag vérifiée dans
un dépôt temporaire, sans modifier les proxies ni lancer la mise à jour réelle.
