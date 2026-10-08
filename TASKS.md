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
