-- C8: approvals and the outbox get the columns they need. Both tables were created empty in 0001 and
-- nothing wrote to them before this version, so they are rebuilt (SQLite cannot change a CHECK in place).

DROP TABLE IF EXISTS approvals;
CREATE TABLE approvals (
  id           TEXT PRIMARY KEY,
  job_id       TEXT REFERENCES jobs(id) ON DELETE CASCADE,
  key          TEXT NOT NULL UNIQUE,          -- "<job>:<step>:<n>": asking again returns the same approval
  plugin       TEXT NOT NULL,
  step         TEXT,
  type         TEXT NOT NULL CHECK (type IN ('entry','batch','draft')),
  title        TEXT NOT NULL,
  payload      TEXT NOT NULL DEFAULT '{}',    -- fields, items, summary, link, and totals computed in code
  state        TEXT NOT NULL CHECK (state IN ('pending','approved','rejected','expired')),
  token_hash   TEXT,                          -- sha256 of the one-time token in the phone buttons; cleared once used
  answer       TEXT,                          -- the fields as approved (edits included), JSON
  expires_at   REAL NOT NULL,
  remind_at    REAL,                          -- cleared once the reminder is sent
  decided_by   TEXT,
  decided_at   REAL,
  created_at   REAL NOT NULL,
  updated_at   REAL NOT NULL
);
CREATE INDEX approvals_state ON approvals (state, expires_at);
CREATE INDEX approvals_job ON approvals (job_id);

DROP TABLE IF EXISTS outbox;
CREATE TABLE outbox (
  id           TEXT PRIMARY KEY,
  kind         TEXT NOT NULL,                 -- ntfy (webhooks later)
  dedupe_key   TEXT UNIQUE,                   -- the same message is never queued twice
  payload      TEXT NOT NULL DEFAULT '{}',
  state        TEXT NOT NULL CHECK (state IN ('pending','sending','sent','failed','skipped')),
  attempts     INTEGER NOT NULL DEFAULT 0,
  next_try_at  REAL NOT NULL,
  last_error   TEXT,
  job_id       TEXT,
  sent_at      REAL,
  created_at   REAL NOT NULL,
  updated_at   REAL NOT NULL
);
CREATE INDEX outbox_due ON outbox (state, next_try_at);
