# SPEC.md — Technical specification (v1 / MVP)

Companion to `README.md` (user-facing) and `CLAUDE.md` (agent-facing).
This is the **source of truth** for data shapes, formulas, and rules. Any disagreement with code → fix the code.

---

## 1. Proxy input format

Depuis le 2026-10-14, les identifiants sont facultatifs : `host:port` est
accepté, y compris avec un préfixe de protocole. Les deux formats peuvent être
mélangés. Sans identifiants, username est vide et le mot de passe vide est
chiffré au repos ; aucun en-tête ni échange d’authentification n’est envoyé.
Une liste blanche IP éventuelle reste une configuration du fournisseur.
La grammaire historique ci-dessous décrit la variante avec identifiants.

```text
<username>:<password>@<host>:<port>
```

### Grammar (EBNF)

```ebnf
line       = username ":" password "@" host ":" port
username   = { ALNUM | "_" | "-" | "." } , 3*64
password   = { PRINTABLE except ":" and "@" }, 1*128
host       = IPv4 | IPv6-bracketed | hostname
port       = DIGIT, 1*5     ; 1..65535
```

### Examples (synthetic — RFC 5737 docs range, fake creds)

```text
USER01xy:pw1AbC@203.0.113.10:41979
USER02ab:pw2DeF@203.0.113.11:46747
```

The parser must:

- Accept blank lines and `#` comments (strip them).
- Ignore leading `http://` / `https://` / `socks5://` schemes if present (store the scheme in `scheme` column; default = `http`).
- Deduplicate by `(scheme, host, port, username)`.
- Round-trip: `parse(serialize(parse(x))) == parse(x)`.

### Record shape (persisted)

| Column            | Type        | Notes                                                      |
|-------------------|-------------|------------------------------------------------------------|
| `id`              | INTEGER PK  | autoincrement                                              |
| `scheme`          | TEXT        | `http` \| `https` \| `socks5` (default `http`)             |
| `host`            | TEXT        | lowercase                                                  |
| `port`            | INTEGER     | 1..65535                                                   |
| `username`        | TEXT        | stored plain (not sensitive on its own)                    |
| `password_enc`    | BLOB        | Fernet-encrypted bytes                                     |
| `label`           | TEXT NULL   | user-supplied tag, optional                                |
| `status`          | TEXT        | `unknown` \| `alive` \| `quarantine` \| `dead`             |
| `score`           | REAL        | 0.0..100.0 (see §4)                                        |
| `latency_ms`      | INTEGER NULL| median of last 20 checks                                   |
| `success_count`   | INTEGER     | total                                                      |
| `fail_count`      | INTEGER     | total                                                      |
| `consec_fails`    | INTEGER     | current streak                                             |
| `last_checked_at` | TEXT NULL   | ISO 8601 UTC                                               |
| `quarantine_until`| TEXT NULL   | ISO 8601 UTC                                               |
| `created_at`      | TEXT        | ISO 8601 UTC                                               |

Index on `(status, score DESC)` for fast rotator pick.

---

## 2. Health checker

### Trigger

- On import.
- Every `HEALTH_CHECK_INTERVAL_S` (default 300 s) for `alive` proxies.
- When `quarantine_until` elapses, for `quarantine` proxies.
- On explicit API call.

### How

- `aiohttp` + `aiohttp_socks` (for SOCKS upstream).
- Concurrency gate: `asyncio.Semaphore(HEALTH_CONCURRENCY)` (default 20).
- Call `GET https://api.ipify.org?format=json` through the proxy.
- Timeout: `HEALTH_TIMEOUT_S` (default 8).
- Success = HTTP 200 + body is a JSON `{"ip": "<the proxy's exit IP>"}`.

Record on each check:

```json
{
  "proxy_id": 42,
  "ok": true,
  "status_code": 200,
  "latency_ms": 412,
  "exit_ip": "203.0.113.10",
  "error": null,
  "checked_at": "2026-10-06T14:12:03Z"
}
```

Keep the last 50 checks per proxy in `proxy_checks` table (for the dashboard sparkline).

---

## 3. Pool manager

In-memory snapshot of DB, refreshed on write. Exposes:

- `active() -> list[Proxy]` — status in `{alive}`, score ≥ `POOL_MIN_SCORE` (default 40).
- `all() -> list[Proxy]`.
- `pick(strategy) -> Proxy | None`.
- `mark_success(proxy_id, latency_ms)`.
- `mark_fail(proxy_id, reason)`.
- `quarantine(proxy_id, minutes=10)`.

---

## 4. Health score formula

```python
# Updated after each outcome:
success_ratio = success_count / max(1, success_count + fail_count)
latency_penalty = clamp(latency_ms / 2000, 0.0, 1.0)   # 2000 ms = full penalty
freshness = exp(-age_minutes / 60)                     # last-check decay

score = 100 * success_ratio * (1 - 0.3 * latency_penalty) * freshness
```

Boundaries:

- `score < POOL_MIN_SCORE (40)` → excluded from rotation but kept.
- `3 consecutive fails` → `quarantine` for 10 min regardless of score.
- `score < 10` after re-test → `dead` (hidden from UI by default, kept in DB).

---

## 5. Rotation strategies

Config in `.env`: `ROTATION_STRATEGY=round_robin|random|weighted|sticky`.

| Strategy      | Pick logic                                                                                   |
|---------------|----------------------------------------------------------------------------------------------|
| `round_robin` | cycle through `pool.active()`                                                                |
| `random`      | `random.choice(pool.active())`                                                               |
| `weighted`    | `random.choices(pool.active(), weights=[p.score for p in ...])`                              |
| `sticky`      | hash `(client_ip, target_domain)` → index; same domain → same proxy for `STICKY_TTL_S` (900) |

Sticky uses an LRU cache (`cachetools.TTLCache`).

---

## 6. Burn detection

A proxy is **burned** on a request if any of:

| Signal              | Rule                                                                                  |
|---------------------|---------------------------------------------------------------------------------------|
| Status code         | response is `401`, `403`, `407`, `429`, `451`, `503`                                  |
| Body contains       | one of `cf-ray`, `captcha`, `access denied`, `unusual traffic` (case-insensitive)     |
| Timeout             | request took `> GATEWAY_TIMEOUT_S` (default 15)                                       |
| Connection refused  | TCP connect fail (ECONNREFUSED, ECONNRESET, ETIMEDOUT)                                |
| Latency spike       | measured latency > 3 × running median of this proxy                                   |

Action:

1. `pool.mark_fail(proxy_id, reason)`.
2. If `consec_fails >= 3` → quarantine 10 min.
3. Retry the user's request on the **next** proxy, up to `MAX_RETRY_PER_REQUEST` (default 2).
4. If all retries fail → return the real upstream error to the client (don't fake 200).

Body-content checks only apply if `BURN_INSPECT_BODY=true` and response is `text/*` or `application/json` and `< 128 KiB`. Larger bodies are proxied as-is without inspection.

---

## 7. Local gateway

### HTTP forward proxy (port 8888)

- Accept `GET`, `POST`, `PUT`, `DELETE`, `HEAD`, `OPTIONS`.
- Handle `CONNECT` (TLS tunnel) by opening a raw TCP tunnel through the upstream proxy.
- Add `Via: 1.1 proxy-rotator` header (strip hop-by-hop headers correctly).
- Chunked transfer passthrough.

### SOCKS5 server (port 1080)

- Methods: no-auth. (v1: no inbound auth on the local gateway. User can enable in v2.)
- Commands: `CONNECT` only. (`BIND` and `UDP ASSOCIATE` not in v1.)
- Resolve DNS **through the upstream** (`ATYP=0x03 domain name` forwarded as-is).

Upstream protocol match:

- Client HTTP → upstream HTTP → use `aiohttp` client with `proxy=`.
- Client HTTP CONNECT → upstream HTTP → raw tunnel via upstream `CONNECT`.
- Client HTTP → upstream SOCKS5 → `aiohttp_socks.ProxyConnector`.
- Client SOCKS5 → upstream HTTP → open TCP tunnel through upstream `CONNECT`.
- Client SOCKS5 → upstream SOCKS5 → wrap in `aiohttp_socks` chain.

---

## 8. Config (`.env`)

| Var                        | Default                    | Meaning                                                         |
|----------------------------|----------------------------|-----------------------------------------------------------------|
| `BIND_HOST`                | `127.0.0.1`                | Interface for UI + gateways                                     |
| `ADMIN_PORT`               | `8080`                     | FastAPI UI                                                      |
| `GATEWAY_HTTP_PORT`        | `8888`                     | Local HTTP proxy                                                |
| `GATEWAY_SOCKS_PORT`       | `1080`                     | Local SOCKS5                                                    |
| `DB_PATH`                  | `./data/proxies.db`        | SQLite file                                                     |
| `MASTER_SECRET`            | *required*                 | Passphrase used to derive the Fernet key                        |
| `HEALTH_CHECK_INTERVAL_S`  | `300`                      | Alive re-test cadence                                           |
| `HEALTH_TIMEOUT_S`         | `8`                        | Per-check timeout                                               |
| `HEALTH_CONCURRENCY`       | `20`                       | Parallel checks                                                 |
| `HEALTH_PROBE_URL`         | `https://api.ipify.org?format=json` | Target URL for health probes                          |
| `POOL_MIN_SCORE`           | `40`                       | Min score to stay in rotation                                   |
| `ROTATION_STRATEGY`        | `weighted`                 | `round_robin\|random\|weighted\|sticky`                         |
| `STICKY_TTL_S`             | `900`                      | How long a sticky mapping stays                                 |
| `GATEWAY_TIMEOUT_S`        | `15`                       | Per-request timeout through gateway                             |
| `MAX_RETRY_PER_REQUEST`    | `2`                        | Rotate N times before giving up on the client request           |
| `BURN_INSPECT_BODY`        | `true`                     | Scan response body for CAPTCHA markers                          |
| `QUARANTINE_MINUTES`       | `10`                       | How long a burned proxy sleeps                                  |
| `LOG_LEVEL`                | `INFO`                     | Standard logging                                                |

---

## 9. Data model (SQLite)

```sql
CREATE TABLE IF NOT EXISTS proxies (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  scheme            TEXT NOT NULL CHECK (scheme IN ('http','https','socks5')),
  host              TEXT NOT NULL,
  port              INTEGER NOT NULL CHECK (port BETWEEN 1 AND 65535),
  username          TEXT NOT NULL,
  password_enc      BLOB NOT NULL,
  label             TEXT,
  status            TEXT NOT NULL DEFAULT 'unknown' CHECK (status IN ('unknown','alive','quarantine','dead')),
  score             REAL NOT NULL DEFAULT 0.0,
  latency_ms        INTEGER,
  success_count     INTEGER NOT NULL DEFAULT 0,
  fail_count        INTEGER NOT NULL DEFAULT 0,
  consec_fails      INTEGER NOT NULL DEFAULT 0,
  last_checked_at   TEXT,
  quarantine_until  TEXT,
  created_at        TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(scheme, host, port, username)
);
CREATE INDEX IF NOT EXISTS idx_proxies_status_score ON proxies(status, score DESC);

CREATE TABLE IF NOT EXISTS proxy_checks (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  proxy_id          INTEGER NOT NULL REFERENCES proxies(id) ON DELETE CASCADE,
  ok                INTEGER NOT NULL CHECK (ok IN (0,1)),
  status_code       INTEGER,
  latency_ms        INTEGER,
  exit_ip           TEXT,
  error             TEXT,
  checked_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_checks_proxy_time ON proxy_checks(proxy_id, checked_at DESC);

CREATE TABLE IF NOT EXISTS geo_cache (
  ip            TEXT PRIMARY KEY,
  country       TEXT,
  country_code  TEXT,
  cached_at     TEXT NOT NULL,
  source        TEXT NOT NULL,
  city TEXT,
  region TEXT
);
CREATE TABLE IF NOT EXISTS provider_tokens (
  provider    TEXT PRIMARY KEY,
  token_enc   BLOB NOT NULL,
  updated_at  TEXT NOT NULL
);
```

Date format everywhere: **ISO 8601 UTC** (`datetime('now')` in SQLite = UTC).

`geo_cache` and `provider_tokens` are formalised by migration 002; both back
opt-in surface (geolocation lookups and the Dichvusocks provider token).

---

## 10. Error handling (user-facing)

- Import 10 proxies, 2 malformed → return `{imported: 0, skipped: 10, errors: [...]}` and write nothing. Atomic API import policy updated for T4.2; the pure parser still reports valid records and line errors separately.
- Pool empty when a client request arrives → `503 Pool Empty` with JSON `{error, pool_size, alive_count}`.
- User removes a proxy that's currently serving a tunneled connection → let the connection finish, mark proxy deleted after close.
- `MASTER_SECRET` missing or wrong key → exit with code `78` (`EX_CONFIG`) and a readable message.

---

## 11. What's **not** in v1

- GUI for config (`.env` only).
- Auth on the admin UI.
- Multi-pool.
- Graph of requests/min.
- Electron shell.
- Automatic subscription-URL polling (user pastes manually for now).

These are listed in `docs/MIGRATION_ELECTRON.md` and `TASKS.md` v2 section.

## 12. City selection (2026-10-14)

RotationSelection accepts nullable city/region strings (128 characters maximum),
trimmed and compared case-insensitively in addition to country, score and pins.
Unknown location fails the filter. Metadata is keyed by the latest observed exit
IP, not the upstream hostname. Existing tunnels continue; all five strategies
respect the selection for new connections.

Migration 003 extends geo_cache with city and region and invalidates old derived
country-only entries. Explicit Dichvusocks imports retain provider location
metadata in that cache. Only cached import metadata is read when external
geolocation is disabled; no lookup is sent. Location remains an estimate and
expires according to the cache TTL. Selection resets on restart.

## Import vérifié (2026-10-14)

Les routes d’import testent chaque nouvelle identité avant stockage. Seuls les
succès sont enregistrés, avec état alive et résultat initial. Les échecs
augmentent failed et skipped ; les doublons sont exclus avant test puis revérifiés
à la transaction. L’import interne sans vérificateur reste disponible pour les
fixtures et outils internes ; les routes utilisateur imposent le vérificateur.
