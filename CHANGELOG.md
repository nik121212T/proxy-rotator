# Changelog

## Post-audit fixes — Palier 1 (2026-10-11)

Full findings and evidence: `AUDIT.md`. Every fix ships with a regression test
that was verified to fail against the previous code.

### Fixed

- **Retries could corrupt the client stream.** A transport failure or a timeout
  occurring *after* the gateway had written a status line caused a second HTTP
  response to be appended to the first one in the same client socket. The
  attempt now tracks whether bytes were committed and never retries past that
  point.
- **Slow transfers were cut at `GATEWAY_TIMEOUT_S`.** The 15 s budget applied to
  the whole transfer, so any download longer than that was truncated (and then
  retried, compounding the bug above) while the proxy was marked failed. The
  per-attempt `wait_for` was replaced by per-read timeouts (`sock_connect` +
  `sock_read`), which also bounds a stalled upstream.
- **Request smuggling (CL.TE).** A request carrying both `Content-Length` and
  `Transfer-Encoding` had its `Content-Length` forwarded while the body was
  decoded as chunked. Framing is now validated per RFC 9112 §6.1/§6.3:
  ambiguous or conflicting framing is rejected with 400 and the connection is
  closed; duplicate identical lengths are collapsed, not rejected.
- **Unbounded read on a signed chunk size.** `chunk-size` is now validated as
  `1*HEXDIG` (RFC 9112 §7.1); `-1` previously reached `StreamReader.read(-1)`,
  which blocked while buffering client data without limit.
- **Non-idempotent requests were replayed.** A POST whose origin replied
  429/403/503 was re-sent through another proxy, risking duplicate side effects.
  POST/PATCH now take a single attempt and the origin's real status is passed
  through to the client; the burn is still recorded against the proxy.
- **Proxies that failed their first check were stranded forever.** `unknown`
  with a past `last_checked_at` matched no branch of `HealthChecker.due()`, so
  it was never retested and never quarantined without manual action. In the live
  database 74 of 100 proxies were in exactly this state.
- **Tunnel successes polluted the latency history.** Every CONNECT recorded
  `mark_success(id, 0)`, so a burst of tunnels collapsed the checks median
  towards zero and inflated the score. Tunnels now record liveness only.
- **500 injected into established tunnels.** Deleting a proxy while a tunnel was
  open raised `KeyError` into the generic handler, which wrote an HTTP 500 into
  a socket already carrying TLS bytes.

### Tests

- Rewrote `test_retry_on_upstream_connect_error_then_success` and
  `test_retry_on_burn_status_then_success`, which asserted a plain 200 and
  would have passed with the retry feature deleted; `test_no_retry_on_2xx` now
  counts upstream attempts.
- Added 9 regression tests covering the failures above.

### Gate

`pytest -q` → 291 passed, 1 skipped, 1 failed; `ruff check app tests` and
`mypy app` clean. The single failure (`test_start_command.py`) requires
`/bin/ps`, which the agent sandbox blocks; it fails identically on the
unmodified tree.

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
