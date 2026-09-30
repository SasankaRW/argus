-- The eval set replayed on demand ("Run tests now") or by the nightly review: one row per run, for the pass-rate
-- history on a playbook's Learning tab.
CREATE TABLE eval_runs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  playbook    TEXT NOT NULL REFERENCES playbooks(key),
  passed      INTEGER NOT NULL,
  total       INTEGER NOT NULL,
  failed      TEXT,                        -- sample ids that didn't match (JSON list)
  tier        TEXT,
  why         TEXT NOT NULL,               -- 'manual' | 'review'
  job_id      TEXT,
  created_at  REAL NOT NULL
);
CREATE INDEX eval_runs_playbook ON eval_runs (playbook, created_at);
