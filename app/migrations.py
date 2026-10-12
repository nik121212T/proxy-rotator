"""Trusted migration SQL, with schema 001 copied verbatim from SPEC.md section 9."""

MIGRATION_001 = """CREATE TABLE IF NOT EXISTS proxies (
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
"""

# Formalises the two sidecar tables previously created outside the migration
# system (audit G5). IF NOT EXISTS keeps it idempotent on databases that
# already have those tables from the ad-hoc ensure_schema() paths.
MIGRATION_002 = """CREATE TABLE IF NOT EXISTS geo_cache (
  ip            TEXT PRIMARY KEY,
  country       TEXT,
  country_code  TEXT,
  cached_at     TEXT NOT NULL,
  source        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS provider_tokens (
  provider    TEXT PRIMARY KEY,
  token_enc   BLOB NOT NULL,
  updated_at  TEXT NOT NULL
);
"""

# This migration contains four simple statements, with no semicolons in literals.
# Each version marker is static SQL: PRAGMA assignment does not accept '?' parameters.
MIGRATIONS: tuple[tuple[str, ...], ...] = (
    (*tuple(sql.strip() for sql in MIGRATION_001.split(";") if sql.strip()), "PRAGMA user_version = 1"),
    (*tuple(sql.strip() for sql in MIGRATION_002.split(";") if sql.strip()), "PRAGMA user_version = 2"),
)
