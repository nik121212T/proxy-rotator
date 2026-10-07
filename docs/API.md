# docs/API.md — REST + WebSocket

Base URL: `http://127.0.0.1:${ADMIN_PORT}` (default `8080`).
All JSON. All times ISO 8601 UTC.

---

## Proxies

### `POST /api/proxies/import`

Import a batch of proxies.

Request:

```json
{
  "text": "USER01xy:pw1AbC@203.0.113.10:41979\nUSER02ab:pw2DeF@203.0.113.11:46747",
  "label": "paid-2026-10"
}
```

Response `200`:

```json
{
  "imported": 2,
  "skipped": 0,
  "errors": []
}
```

An import with any line error writes nothing (atomic batch policy).
`imported` is zero, `skipped` counts all non-comment records, and
`errors[i] = {"line": 4, "reason": "Invalid proxy format"}` preserves each parse error.
`line` is the 1-based physical line number, never the submitted credential-bearing text.
Blank lines and full-line `#` comments are ignored. Duplicate identities
`(scheme, host, port, username)` count as skipped; the first record wins.
Only the first 2000 physical lines are parsed. Later nonblank, noncomment records
count as skipped, with one limit error identifying the first such overflow line.
The parser's accepted records are internal only and excluded from report JSON.
A clean batch is committed in one transaction; existing identities are skipped
without replacing their credentials or label. SQLite write failures roll back
all insertions and publish no events.

### `GET /api/proxies`

Query params : `status`, `min_score`, `limit`, `offset`.
Returns `{items: [Proxy], total: int}`. Passwords never returned.
Default limit is 100, offset 0; total counts matches before pagination.
Dead proxies are hidden unless `status=dead` is explicitly requested.

### `GET /api/proxies/{id}`

Proxy fields plus `checks` containing the latest 50 results, newest first. Unknown IDs return 404.

### `POST /api/proxies/{id}/test`

Fire a one-off health check. Response : `CheckResult`.

### `POST /api/proxies/test-all`

Response : `{queued: n}`. Progress via WebSocket.

### `DELETE /api/proxies/{id}`

Soft-delete. Returns `204` with no body, or 404 for an unknown ID.
For the existing v1 schema, soft-delete means status=dead, retained credentials
and history, and no automatic retest. An in-flight probe is cancelled first.
Explicit test-one/test-all can revive a dead proxy. Gateway tunnel draining is
reserved for Phase 5. Hard deletion of dead records is `/api/pool/clear-dead`.

Validation errors return a sanitized 422 response without echoing submitted input.

---

## Pool

### `GET /api/pool/stats`

```json
{
  "total": 50,
  "alive": 34,
  "quarantine": 4,
  "dead": 12,
  "avg_score": 61.2,
  "avg_latency_ms": 480,
  "strategy": "weighted"
}
```

### `POST /api/pool/clear-dead`

Delete all `status=dead` rows. Returns `{deleted: n}`.

### `POST /api/pool/strategy`

Body : `{strategy: "weighted"}`. Returns the new stats.

---

## Gateway

### `GET /api/gateway/status`

```json
{
  "http": {"configured": "127.0.0.1:8888", "bound": null, "active_conns": 0, "requests_total": 0},
  "socks5": {"configured": "127.0.0.1:1080", "bound": null, "active_conns": 0, "requests_total": 0}
}
```

---

Phase 4 reports configured addresses but opens no gateway sockets. Phase 5 will
replace the runtime gateway snapshot with bound addresses and actual counters.

## WebSocket `/ws/events`

Each WebSocket frame contains one JSON object `{type, payload}`.
A snapshot of pool_stats is sent on connection, on change and every 2 seconds.
Each client has its own 100-event queue; saturation drops the oldest event.
This is a live channel, not a durable/replayable log: refresh REST snapshots
on reconnect (and periodically) to recover dropped changes. Disconnect cancels
sender/receiver tasks and unsubscribes. `gateway_event` is an empty-payload hook
until Phase 5 supplies gateway events.

Event types :

| `type`           | Payload                                                                   |
|------------------|---------------------------------------------------------------------------|
| `check_result`   | `{proxy_id, ok, latency_ms, status_code, error}`                          |
| `proxy_updated`  | `{proxy_id, status, score, consec_fails}`                                 |
| `proxy_removed`  | `{proxy_id}`                                                              |
| `pool_stats`     | same shape as `GET /api/pool/stats` (every 2 s or on change)              |
| `gateway_event`  | `{kind: "request_start"\|"request_end", proxy_id, target, latency_ms?}`   |


## Dashboard / HTMX

`GET /` renders the Jinja2 dashboard. Assets are served from `/static`; HTMX
2.0.11 is vendored with its license. There are no runtime CDN/font requests.
Forms use the local `json-form` HTMX extension and submit the same JSON payloads
as REST clients. A request with `HX-Request: true` receives escaped HTML feedback
for import, test-one, test-all, clear-dead and strategy; ordinary requests retain
the JSON contracts above. DELETE remains 204. `HX-Trigger: pool-refresh` requests
a UI resync. Successful imports clear the credential textarea; line errors keep
it for correction. Validation errors remain sanitized 422 JSON and the browser
shows a fixed human-readable message.

`GET /ui/proxies/{id}?append=true|false` renders an out-of-band row replacement,
append or deletion. `GET /ui/snapshot` renders authoritative table/summary OOB
fragments. The client serializes/debounces fragment requests, refreshes snapshots
at reconnect and periodically to recover dropped WebSocket events, preserves
keyboard focus and unsaved strategy edits, and defers row refreshes during actions.
