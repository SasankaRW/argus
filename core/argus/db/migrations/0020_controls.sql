-- Component controls (Helios: stop, start, restart, pause). Runtime state that survives an Argus restart; it never
-- replaces plugins.off in argus.yaml (that stays the config's say).

-- A plugin paused at runtime: its queued jobs are not claimed, nothing new is queued for it. `generation` goes up on
-- every restart; a worker that sees a new number reloads the plugin's code before its next job.
CREATE TABLE plugin_control (
  plugin      TEXT PRIMARY KEY,
  paused      INTEGER NOT NULL DEFAULT 0,
  since       REAL,
  reason      TEXT,
  by_all      INTEGER NOT NULL DEFAULT 0,   -- paused by "pause everything" (so "resume everything" puts it back)
  generation  INTEGER NOT NULL DEFAULT 0,
  updated_at  REAL NOT NULL
);

-- A worker (a box on the map: its fast lane and session twin count with it) told to stop claiming (it keeps
-- heartbeating and finishes the job it has), or to restart itself between jobs.
CREATE TABLE worker_control (
  worker             TEXT PRIMARY KEY,
  paused             INTEGER NOT NULL DEFAULT 0,
  since              REAL,
  reason             TEXT,
  by_all             INTEGER NOT NULL DEFAULT 0,
  restart_pending    TEXT,                      -- JSON list: worker processes not yet told to restart
  updated_at         REAL NOT NULL
);

-- Small flags: paused_all = "pause everything" is in force.
CREATE TABLE control_flags (
  key         TEXT PRIMARY KEY,
  value       TEXT,
  updated_at  REAL NOT NULL
);
