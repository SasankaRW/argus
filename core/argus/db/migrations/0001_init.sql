-- Argus schema v1: the ten core tables. Times are Unix seconds (REAL). IDs are ULIDs.

CREATE TABLE jobs (
  id            TEXT PRIMARY KEY,
  plugin        TEXT NOT NULL,
  workflow      TEXT NOT NULL,
  state         TEXT NOT NULL CHECK (state IN
                  ('queued','leased','running','waiting','retry','succeeded','dead','cancelled')),
  priority      INTEGER NOT NULL DEFAULT 50,
  needs         TEXT NOT NULL DEFAULT '[]',
  dedupe_key    TEXT,
  attempt       INTEGER NOT NULL DEFAULT 0,
  max_attempts  INTEGER NOT NULL DEFAULT 3,
  run_after     REAL NOT NULL,
  lease_owner   TEXT,
  lease_until   REAL,
  wait_reason   TEXT,
  input         TEXT NOT NULL DEFAULT '{}',
  result        TEXT,
  error         TEXT,
  created_at    REAL NOT NULL,
  updated_at    REAL NOT NULL,
  finished_at   REAL
);
CREATE INDEX jobs_claim ON jobs (state, priority DESC, run_after, created_at);
CREATE INDEX jobs_plugin_state ON jobs (plugin, state);
CREATE INDEX jobs_lease ON jobs (state, lease_until);
CREATE UNIQUE INDEX jobs_dedupe_active ON jobs (dedupe_key)
  WHERE dedupe_key IS NOT NULL AND state IN ('queued','leased','running','waiting','retry');

CREATE TABLE steps (
  job_id       TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  idx          INTEGER NOT NULL,
  name         TEXT NOT NULL,
  state        TEXT NOT NULL CHECK (state IN ('running','succeeded','failed')),
  tier_used    TEXT,
  output       TEXT,
  error        TEXT,
  started_at   REAL,
  finished_at  REAL,
  created_at   REAL NOT NULL,
  updated_at   REAL NOT NULL,
  PRIMARY KEY (job_id, idx)
);

CREATE TABLE events (
  id              TEXT PRIMARY KEY,
  job_id          TEXT,
  step            TEXT,
  kind            TEXT NOT NULL,
  from_component  TEXT,
  to_component    TEXT,
  data            TEXT,
  at              REAL NOT NULL,
  created_at      REAL NOT NULL,
  updated_at      REAL NOT NULL
);
CREATE INDEX events_at ON events (at);
CREATE INDEX events_job ON events (job_id);

CREATE TABLE approvals (
  id          TEXT PRIMARY KEY,
  job_id      TEXT REFERENCES jobs(id) ON DELETE CASCADE,
  type        TEXT NOT NULL CHECK (type IN ('entry','batch','draft')),
  payload     TEXT NOT NULL DEFAULT '{}',
  state       TEXT NOT NULL CHECK (state IN ('pending','approved','rejected','expired')),
  token_hash  TEXT,
  expires_at  REAL,
  decided_by  TEXT,
  decided_at  REAL,
  created_at  REAL NOT NULL,
  updated_at  REAL NOT NULL
);
CREATE INDEX approvals_state ON approvals (state, expires_at);

CREATE TABLE outbox (
  id           TEXT PRIMARY KEY,
  kind         TEXT NOT NULL,
  payload      TEXT NOT NULL DEFAULT '{}',
  state        TEXT NOT NULL CHECK (state IN ('pending','sent','failed')),
  attempts     INTEGER NOT NULL DEFAULT 0,
  next_try_at  REAL NOT NULL,
  last_error   TEXT,
  created_at   REAL NOT NULL,
  updated_at   REAL NOT NULL
);
CREATE INDEX outbox_due ON outbox (state, next_try_at);

CREATE TABLE workers (
  id            TEXT PRIMARY KEY,
  host          TEXT NOT NULL,
  capabilities  TEXT NOT NULL DEFAULT '[]',
  version       TEXT,
  last_seen     REAL,
  state         TEXT NOT NULL DEFAULT 'offline' CHECK (state IN ('online','offline')),
  created_at    REAL NOT NULL,
  updated_at    REAL NOT NULL
);

CREATE TABLE components (
  id          TEXT PRIMARY KEY,
  kind        TEXT NOT NULL CHECK (kind IN ('core','plugin','model','app','worker','service')),
  label       TEXT NOT NULL,
  grp         TEXT,
  meta        TEXT NOT NULL DEFAULT '{}',
  first_seen  REAL NOT NULL,
  created_at  REAL NOT NULL,
  updated_at  REAL NOT NULL
);

CREATE TABLE schedules (
  id           TEXT PRIMARY KEY,
  plugin       TEXT NOT NULL,
  cron         TEXT NOT NULL,
  next_run_at  REAL,
  last_run_at  REAL,
  enabled      INTEGER NOT NULL DEFAULT 1,
  created_at   REAL NOT NULL,
  updated_at   REAL NOT NULL
);

CREATE TABLE plugin_state (
  plugin      TEXT NOT NULL,
  key         TEXT NOT NULL,
  value       TEXT,
  created_at  REAL NOT NULL,
  updated_at  REAL NOT NULL,
  PRIMARY KEY (plugin, key)
);

CREATE TABLE settings (
  key         TEXT PRIMARY KEY,
  value       TEXT,
  created_at  REAL NOT NULL,
  updated_at  REAL NOT NULL
);
