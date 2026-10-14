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
  "with_auth": 1,
  "without_auth": 1,
  "skipped": 0,
  "errors": []
}
```

Invalid lines are skipped and reported; valid records are imported together in one database transaction.
`with_auth` and `without_auth` split newly imported records by credential presence;
they exclude duplicates and sum to `imported`. Both are zero when nothing is added.
`imported` counts valid new records, `skipped` counts invalid/duplicate records, and
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

Response: `{queued: n}`. One manual batch at a time: repeated requests during an
active batch return `{queued: 0}` and do not restart completed members. The first
response counts participating proxies, including already-running checks that
are shared with the batch. Each probe has its full network timeout after its
semaphore slot becomes available; queue waiting never records a proxy failure.

HTMX receives the `check-controls` fragment, containing a disabled button during
work, completed/total counters and safe error counts. `GET /ui/check-status`
refreshes that fragment every two seconds, including a native progress bar and outcome totals. It also restores progress after reload.
WebSocket events continue updating proxy rows and pool statistics.

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
  "with_auth": 40,
  "without_auth": 10,
  "alive": 34,
  "quarantine": 4,
  "dead": 12,
  "avg_score": 61.2,
  "avg_latency_ms": 480,
  "strategy": "weighted"
}
```

`with_auth` and `without_auth` count the entire stored pool across all statuses,
independent of dashboard display filters, and sum to `total`.

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

Dashboard and `/ui/snapshot` accept `sort=best|issues|latency|score|success|recent|added` (unknown values fall back to best). Sorting is stable by proxy ID for ties; unmeasured latency and success rates sort last. Live snapshots preserve the current sort and refresh gateway connection/request counters.

## Live usage and selection

`GET /api/gateway/activity` returns `active` and `recent` (up to 20 completed
connections). Items contain connection_id, proxy_id, gateway, started_at,
duration_s, public proxy metadata, last verified exit observation and country.
Exit observations come from health probes, not decryption of HTTPS tunnels.

`POST /api/pool/selection`: `{ "country": "FR", "min_score": 80, "pinned_only": false }`.
Country can be null; score is 0–100. Filters apply to new connections only and
fail closed when no eligible proxy remains. Filters and pins reset on restart.

`POST /api/gateway/http/test` and `POST /api/gateway/socks5/test` send the configured
health probe through the corresponding bound listener. Result: `ok`, `exit_ip`
(null on failure), `latency_ms`, `message`. Deadline 10 seconds, no redirects or
direct fallback. Concurrent duplicate tests return 409. Tests consume a normal
rotation selection and contribute to gateway counters/activity.

Timed rotation: `POST /api/pool/strategy` also accepts
`{"strategy":"timed","interval_minutes":5}` (integer 1–1440, default 1).
The interval starts when a proxy is selected. On the first request after expiry,
the next eligible proxy is selected; an unavailable proxy is replaced immediately.
Existing tunnels are never forcibly closed. Applying the strategy resets the timer.
Only one eligible proxy means it will be reused; a different exit IP is not guaranteed.
The dashboard interval resets to one minute on application restart.

### City and region selection

`POST /api/pool/selection` additionally accepts `city` and `region` (nullable
strings, at most 128 characters). Example:
`{"country":"FR","city":"Paris","region":"Île-de-France","min_score":40}`.
Whitespace-only fields become null. Matching is case-insensitive and combines
all filters; unknown/mismatched locations are excluded with no fallback.
`GET /api/geo/{ip}` includes `city` and `region` when geolocation is enabled.

Dichvusocks preview/import query parameters `city` and `region` are exposed in
the settings form. Explicit imports cache the provider's location metadata;
rotation uses it only for a matching health-observed exit IP. Cached import
metadata is also available internally when external geolocation is disabled.

Dashboard and `/ui/snapshot` also accept `sort=city`: alphabetical city order,
case/accent-insensitive, unknown cities last, stable proxy-ID tie breaker.
Rows display both city and country from the latest observed exit IP.

### Dashboard display filters

`GET /`, `/ui/snapshot` and `/ui/proxies/{id}` accept `city`, `country`, `status`
in addition to `sort`. City is an accent/case-insensitive substring; country is
an ISO alpha-2 code; status is `alive|unknown|quarantine|dead|all`. An omitted or
invalid status hides dead proxies. These filters never change RotationSelection.
Snapshots include result counts and known-location suggestions. Row fragments
remove proxies that do not match the current filters.

`POST /api/pool/strategy` also accepts optional boolean `pinned_only`, preserving
location/score filters. Omitting it preserves the current priority restriction.
`POST /api/pool/selection` likewise preserves `pinned_only` when that field is
omitted; an explicit boolean still updates it. The checkbox now belongs to the
Rotation form and is saved by “Appliquer la stratégie”.

When `pinned_only` is enabled, the control is locked to `round_robin` and both
strategy/selection endpoints enforce that mode. Disable the restriction before
choosing another strategy. The form explicitly serializes the disabled select.
