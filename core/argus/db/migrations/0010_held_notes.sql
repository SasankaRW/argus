-- Quiet by default: plugin messages that aren't urgent wait here for the evening summary instead of buzzing
-- the phone at once (ntfy.quiet). Approvals, failures, reminders and warnings are never held.
CREATE TABLE held_notes (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id      TEXT,
  plugin      TEXT,
  title       TEXT NOT NULL,
  text        TEXT NOT NULL DEFAULT '',
  dedupe_key  TEXT UNIQUE,
  created_at  REAL NOT NULL,
  told_at     REAL                      -- when an evening summary included it
);
CREATE INDEX held_notes_open ON held_notes (told_at, created_at);
