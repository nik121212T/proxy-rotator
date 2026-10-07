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
- [ ] T0.4  [deferred to T6.1] `./scripts/start.sh` and `scripts\start.bat` print a usage message and exit 0 when deps missing.
  - Non-blocking launcher UX deferred to T6.1/T6.2 by the user. Continue Phase 1 locally, without a GitHub remote or push.

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
  - Real local HTTP, authenticated SOCKS5 (remote DNS), and verified TLS-over-CONNECT tests; 50-proxy batch, bounded concurrency, import scheduling, quarantine retests and cancellation. Configurable batch deadline resolves the 10 s target versus queued 8 s probes; unfinished probes receive explicit failed results. Startup wiring remains T4.1.

## Phase 4 — API + UI

- [x] T4.1  `app/main.py` : FastAPI app, lifespan, mount routers.
  - Validated startup settings, off-thread key derivation, credential preflight, migrated DB, pool/rotator/checker lifecycle and readiness. Empty API routers mounted for T4.2–T4.4. `python -m app.main` maps configuration/crypto startup failures to EX_CONFIG=78; launcher scripts remain T6.1/T6.2. Local HTTP and real SQLite lifecycle tests.
- [x] T4.2  `app/routes/proxies.py` : REST per `docs/API.md`.
  - Atomic imports (any line error rejects the batch per latest user instruction), first-wins identity dedup, filtered pages/details, queued and individual checks, dead-state soft delete. Sanitized validation errors; real DB rollback/concurrency and 60-check retention tests. Fixed fake aiohttp handler leaks; test teardown rejects leaked tasks and warnings.
- [x] T4.3  `app/routes/pool.py` : stats + control endpoints.
  - Snapshot-only aggregate stats, validated rotation changes with pool_stats events, atomic dead-only purge preserving alive/quarantined records.
- [ ] T4.4  `app/routes/gateway.py` : gateway status / strategy switch.
- [ ] T4.5  WebSocket `/ws/events` broadcasts pool events.
- [ ] T4.6  Dashboard HTML (Jinja2 + HTMX) : table of proxies, forms, live updates via WS.

## Phase 5 — Local gateways

- [ ] T5.1  `app/gateway_http.py` : HTTP forward proxy + CONNECT tunnel.
- [ ] T5.2  Burn-on-response hook + automatic retry on next proxy (≤ `MAX_RETRY_PER_REQUEST`).
- [ ] T5.3  `app/gateway_socks.py` : SOCKS5 no-auth CONNECT server with DNS passthrough.
- [ ] T5.4  Smoke test : `curl -x http://127.0.0.1:8888 https://api.ipify.org` returns a different IP from the user's direct IP, three times.

## Phase 6 — Packaging

- [ ] T6.1  `scripts/start.sh` : auto-create venv if missing, install deps, start.
- [ ] T6.2  `scripts/start.bat` : same for Windows.
- [ ] T6.3  GitHub Actions matrix (ubuntu-latest, macos-latest, windows-latest) : `pip install`, `pytest`, `ruff`, `mypy`.
- [ ] T6.4  README screenshot or ASCII sketch of dashboard added.

## Phase 7 — Done gate

- [ ] T7.1  All above checked.
- [ ] T7.2  Changelog in PR description.
- [ ] T7.3  Three-rotation proof pasted in PR.

---

## v2 — Deferred (NOT in v1, listed for context)

- Electron shell wrapping the FastAPI backend.
- Multi-pool regional routing.
- Dashboard graphs (requests/min, success % over 1 h).
- Subscription URL auto-poll + diff.
- Sticky session per client cookie.
- Basic auth on the admin UI.
- Browser-config helper (Firefox autoconfig, SwitchyOmega preset).
