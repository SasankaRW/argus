-- The guidance loop: what the models answered (samples), your verdicts (correct / wrong: the eval set), and the
-- lessons a nightly review proposes and you approve (appended to that playbook for that plugin from then on).
CREATE TABLE playbooks (
  key         TEXT PRIMARY KEY,           -- plugin:sha1(playbook text)[:12]
  plugin      TEXT NOT NULL,
  name        TEXT,                        -- the playbook's first line, for people
  text        TEXT NOT NULL,
  schema      TEXT,                        -- the JSON schema the answer had to match
  first_seen  REAL NOT NULL,
  last_seen   REAL NOT NULL
);
CREATE TABLE samples (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id      TEXT,
  plugin      TEXT NOT NULL,
  playbook    TEXT NOT NULL REFERENCES playbooks(key),
  input       TEXT NOT NULL,
  output      TEXT NOT NULL,
  tier        TEXT,
  escalated   INTEGER NOT NULL DEFAULT 0,  -- the first tier's answer was rejected
  verdict     TEXT CHECK (verdict IN ('correct', 'wrong')),
  correction  TEXT,                        -- for a wrong one: what it should have been (JSON or words)
  reviewed_at REAL,
  created_at  REAL NOT NULL
);
CREATE INDEX samples_playbook ON samples (playbook, created_at);
CREATE INDEX samples_job ON samples (job_id);
CREATE TABLE lessons (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  playbook    TEXT NOT NULL REFERENCES playbooks(key),
  text        TEXT NOT NULL,               -- the lines appended to the playbook
  state       TEXT NOT NULL CHECK (state IN ('proposed', 'active', 'rejected', 'replaced')),
  evals       TEXT,                        -- {before, after} pass rates on the eval set
  job_id      TEXT,
  created_at  REAL NOT NULL,
  decided_at  REAL
);
CREATE INDEX lessons_playbook ON lessons (playbook, state);
