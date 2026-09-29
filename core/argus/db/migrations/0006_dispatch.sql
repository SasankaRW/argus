-- C9: scheduler, triggers and dispatcher.

-- The model a job mostly uses (GPU jobs are grouped by it, so Ollama swaps models rarely) and the window it
-- may start in (e.g. night).
ALTER TABLE jobs ADD COLUMN model_group TEXT;
ALTER TABLE jobs ADD COLUMN run_window TEXT;
CREATE INDEX jobs_plugin_running ON jobs (plugin, state) WHERE state IN ('leased','running');

-- Schedules come from argus.yaml (and plugin manifests later); this table keeps their clock.
DROP TABLE IF EXISTS schedules;
CREATE TABLE schedules (
  id           TEXT PRIMARY KEY,
  plugin       TEXT NOT NULL,
  workflow     TEXT NOT NULL,
  cron         TEXT NOT NULL,
  spec         TEXT NOT NULL DEFAULT '{}',   -- input, needs, priority, model, window
  enabled      INTEGER NOT NULL DEFAULT 1,
  next_run_at  REAL,
  last_run_at  REAL,
  last_job_id  TEXT,
  created_at   REAL NOT NULL,
  updated_at   REAL NOT NULL
);

-- Files a folder trigger has already handed to a plugin, by content: the same file again (a copy, a
-- re-download, a restart of the watcher) is merged instead of processed twice.
CREATE TABLE files_seen (
  plugin      TEXT NOT NULL,
  sha256      TEXT NOT NULL,
  path        TEXT NOT NULL,
  trigger     TEXT NOT NULL,
  job_id      TEXT,
  created_at  REAL NOT NULL,
  updated_at  REAL NOT NULL,
  PRIMARY KEY (plugin, sha256)
);
