# Changelog

## v1 MVP — 2026-10-08

Local delivery; no remote repository, push or hosted release. This summarizes
23 development commits after the initial bootstrap, from `977b3aa` through
`70e79b5`, plus the subsequent smoke-test and packaging commits.

### Delivered

- Bootstrap and task clarifications: Python 3.11 environment, smoke collection,
  lint/types on real code; Windows execution deferred to hosted CI.
- Core: deterministic proxy parser, Hypothesis round trips, bounded/deduplicated
  bulk reports, immutable Pydantic DTOs, Fernet encryption and persisted PBKDF2 salt.
- Storage: async SQLite with WAL/foreign keys, exact versioned schema,
  encrypted repositories, atomic batch transactions and 50-check retention.
- Domain: immutable pool snapshots with serialized persistence, event fanout,
  freshness/latency scoring, four rotation strategies, burn classification,
  quarantine/retests and bounded concurrent health checks.
- API/UI: managed service lifespan, proxy management, pool controls, gateway
  status, bounded WebSocket subscriptions and a local HTMX/Jinja dashboard.
- Gateways: HTTP forwarding and CONNECT, burn/retry integration, SOCKS5 CONNECT
  with upstream DNS, fail-closed empty pools and listener lifecycle management.
- Packaging: three-request HTTP rotation smoke, robust macOS/Windows bootstrap,
  Python 3.11 CI matrix for Ubuntu/macOS/Windows, and README dashboard sketch.
- Test cleanup: fake tunnel upstreams explicitly close accepted writers;
  teardown collects resources and rejects surviving background tasks.

### Validation and release limits

- Local Python 3.11: **264 passed, 1 skipped**; Ruff and mypy pass. The skipped
  test executes the Windows batch launcher and is enabled on Windows CI.
- All Phase 0–6 implementation boxes are checked. Hosted CI execution is
  **SKIPPED** until a GitHub repository exists; no Windows success is claimed.
- Three-rotation proof: **requires live upstream proxies**. Smoke exits 1 with
  `Missing .env: configure MASTER_SECRET and import live upstream proxies first.`
  Offline harnesses verify success, repeated-IP failure and process cleanup.
- Known UI debt: Gateway still displays configured addresses and a static
  “Non démarrées” label. `/api/gateway/status` exposes actual bound addresses
  and counters. Track the UI follow-up as an issue when the remote exists.
- Existing maintenance debt: dashboard resync renders the visible pool in full;
  AnyIO is pinned for the current Starlette/TestClient combination. No framework
  or dependency upgrade is included in this packaging pass.

The requested local work is complete. A cross-platform release claim still
requires hosted CI and a successful smoke run with real upstream proxies.
