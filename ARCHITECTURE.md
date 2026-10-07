# ARCHITECTURE.md

Component map, data flow, concurrency model. Companion to `SPEC.md`.

---

## Component diagram

```
                             ┌───────────────────────────┐
                             │  Admin UI (HTMX)          │
                             │  http://127.0.0.1:8080    │
                             └────────────┬──────────────┘
                                          │ REST + WebSocket
                                          ▼
┌───────────────────┐          ┌─────────────────────────────┐
│ Browser / app     │ 8888 →   │                             │
│ (user traffic)    │ 1080 →   │     Local Gateway Layer     │
└───────────────────┘          │                             │
                                │ gateway_http.py            │
                                │ gateway_socks.py           │
                                └──────────────┬──────────────┘
                                               │ ask Rotator
                                               ▼
                               ┌─────────────────────────────┐
                               │     Pool / Rotator          │
                               │  pool.py · rotator.py       │
                               │  (in-memory + SQLite sync)  │
                               └──────────────┬──────────────┘
                                              │ reads/writes
                                              ▼
                               ┌─────────────────────────────┐
                               │        SQLite (aiosqlite)   │
                               │  data/proxies.db            │
                               └──────────────▲──────────────┘
                                              │
                                              │ writes outcomes
┌───────────────────────────┐   schedules     │
│   Health Checker          │ ────────────────┘
│   health_checker.py       │
│   (async loop)            │
└──────────┬────────────────┘
           │ probes
           ▼
     Upstream proxies (user's paid list)
```

---

## Layer boundaries

| Layer    | Modules                            | Owns                                               | Does NOT                               |
|----------|------------------------------------|----------------------------------------------------|----------------------------------------|
| Core     | `parser.py`, `crypto.py`, `models.py` | Pure functions. Data shapes. Encryption primitives. | Any I/O.                               |
| Storage  | `db.py`, `migrations.py`, `repository.py`, `salt.py` | SQLite lifecycle, versioned schema, DTO persistence, derivation salt. | Business logic. |
| Domain   | `pool.py`, `rotator.py`, `health_checker.py`, `burn_detector.py` | Scoring, selection, probing, burn rules.           | Protocol specifics (HTTP/SOCKS).       |
| Protocol | `gateway_http.py`, `gateway_socks.py` | TCP accept, parse HTTP/SOCKS, tunnel.             | Scoring / policy. Asks Pool for a proxy. |
| API      | `routes/*.py`                      | REST endpoints, WebSocket.                         | Business logic (delegates to Domain).  |
| UI       | `ui/templates/*`, `ui/static/*`    | HTMX-rendered pages.                               | Business logic.                        |

Dependency arrows go **down**, never up. Protocol depends on Domain; Domain never depends on Protocol.

---

## Concurrency model

Single asyncio event loop, started by Uvicorn. All I/O (SQLite, HTTP checks, TCP tunnels) is non-blocking.

| Task group               | Owner           | Count            | Backpressure                        |
|--------------------------|-----------------|------------------|-------------------------------------|
| Health check jobs        | HealthChecker   | ≤ `HEALTH_CONCURRENCY` (20) | `asyncio.Semaphore`           |
| Gateway inbound conns    | TCP server      | unbounded (OS)   | per-conn `asyncio.Lock` on upstream |
| Upstream conns per req   | Gateway         | 1                | `aiohttp.ClientSession` pooling     |
| Admin API                | FastAPI         | unbounded        | Rate limit N/A for localhost        |
| WebSocket broadcasts     | PoolEventBus    | 1 fanout         | `asyncio.Queue` per subscriber      |

CPU work (parsing bodies for burn detection) runs on the loop (< 128 KiB). If future features need heavier work, offload to `ProcessPoolExecutor`.

---

## Data flow for one client request through HTTP gateway

```
Browser  → 127.0.0.1:8888    (CONNECT google.com:443)
            │
            │ gateway_http.accept()
            ▼
         Rotator.pick(strategy)       ─── Pool: ["#17 alive 82.3", "#9 alive 71.5", …]
            │
            │ chosen: proxy #17
            ▼
         aiohttp CONNECT google.com:443 via proxy #17
            │
            │ tunnel OK
            ▼
         Pipe bytes both directions
            │
            │ on close:
            ▼
         Pool.mark_success(#17, latency_ms=312)
```

On failure:

```
         aiohttp raises Timeout / 403 / RST
            │
            ▼
         BurnDetector.classify(err) → "burn: 429"
            │
            ▼
         Pool.mark_fail(#17, "429")
            │   if consec_fails ≥ 3 → quarantine
            ▼
         retry_count < MAX_RETRY_PER_REQUEST ?
            │ yes → Rotator.pick() → new proxy, retry
            │ no  → 502 Bad Gateway to client
```

---

## Startup sequence

1. Load `.env` via `pydantic-settings`.
2. Derive Fernet key from `MASTER_SECRET` (`PBKDF2HMAC`, salt = file in `data/`).
3. Open SQLite, run migrations.
4. Load proxies into in-memory `Pool`.
5. Start `HealthChecker` background task.
6. Start Uvicorn (admin UI + REST).
7. Start HTTP gateway on `GATEWAY_HTTP_PORT`.
8. Start SOCKS5 gateway on `GATEWAY_SOCKS_PORT`.
9. Log one `READY` line with all three bound addresses.

Shutdown: cancel background tasks → close gateway listeners → drain in-flight → close DB → exit.

### Storage transactions

`get_db(path)` owns one async connection, enables foreign keys and WAL, and
applies pending migrations atomically using `PRAGMA user_version`. Connections
use autocommit. A current schema is checked without acquiring a writer lock.

Each task owns its connection; do not interleave independent tasks on a
connection during a transaction. For bulk import, use one
`async with transaction(conn)` around all insert/upsert calls. A failure or
cancellation rolls the entire batch back; individual repository functions do
not commit an enclosing batch.

Repository input values use bound parameters. Inserts and upserts encrypt
`ParsedProxy.password` before SQLite sees it; encryption runs off the event
loop. Upsert updates credentials and label, preserving ID, creation time and
health history. Queries return `Proxy` DTOs, in ID order, with optional
`ProxyFilters(status, min_score, limit, offset)`. Storage includes all statuses
by default; the UI's dead-proxy visibility policy belongs to its caller.

`record_check` returns a `CheckResult` and atomically inserts/prunes to the
latest 50 checks per proxy. A savepoint preserves any enclosing transaction.
Check timestamps are written as fixed-precision ISO 8601 UTC; SQLite's UTC
timestamps without offsets are normalized when reading proxy DTOs. Check
error messages must already be sanitized by the health-check producer.

`delete_proxy` returns the removed DTO or `None`; foreign keys cascade checks.
The domain must first drain active tunnels before calling this hard-delete
operation (SPEC §10).

`salt.load_or_create_salt()` owns salt-file I/O; `crypto.py` accepts salt bytes
and a `SecretStr`, keeping filesystem and environment access out of Core.
Key derivation uses PBKDF2HMAC-SHA256, 480,000 iterations and a 32-byte output.
The startup integration must offload salt I/O and derivation from the event loop
and translate `CryptoConfigError.exit_code` to process exit 78 (`EX_CONFIG`).

---

## Domain lifecycle and health timing

After loading, Pool exclusively owns its connection. Its mutation lock serializes
write-through transactions before publishing immutable snapshots and events.
Readers take tuple snapshots without locking. Each subscriber has a bounded
queue; overflow requires resubscription and a snapshot reload. The health loop
does this automatically. Application startup and shutdown wiring belongs to T4.1.

Selection recalculates score freshness without mutating stored snapshots.
Latency uses the rounded median of the latest 20 measurements, restoring check
history from SQLite on load. Rotators only select active snapshots; an empty
selection never permits a direct connection. Burn classification is pure;
gateway response handling and retries remain T5.2.

Health checks use aiohttp and aiohttp-socks, verified TLS, explicit upstreams,
and SOCKS remote DNS. They ignore environment proxy settings. Concurrent checks
for the same proxy share one job. The background loop checks newly imported
proxies, alive proxies every 300 seconds, and quarantined proxies when due;
dead proxies require an explicit check. Configuration/decryption failures
propagate rather than counting against proxy health.

The per-probe timeout stays 8 seconds and concurrency stays 20. Fifty stalled
probes would otherwise take three waves, exceeding the 10-second import target.
The implementation therefore adds a configurable 10-second batch budget:
90% for network work (including semaphore waits), reserving the remainder for
durable results. Unfinished checks produce a sanitized batch-deadline failure,
including checks still queued. Persistence is awaited, so a stalled disk can
exceed the budget; this is not a hard real-time guarantee. Local fake-upstream
tests verify that 50 successful probes complete within 10 seconds. No external
service is contacted by tests.

## Observability

- **Logs**: stdout, structured, one event per line, key=value.
- **Metrics**: in-memory counters surfaced on `GET /api/pool/stats`. No Prometheus in v1.
- **WebSocket `/ws/events`**: broadcasts proxy state transitions + health check results for the dashboard.
