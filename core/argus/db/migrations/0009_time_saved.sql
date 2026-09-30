-- Time saved: plugins estimate the minutes a job saved you (ctx.saved). One row per job and key, so a retried job
-- doesn't count twice. Kept for good (small); Helios shows the week, the evening summary the day.
CREATE TABLE time_saved (
  job_id      TEXT NOT NULL,
  key         TEXT NOT NULL,
  plugin      TEXT NOT NULL,
  day         TEXT NOT NULL,          -- local date, YYYY-MM-DD
  seconds     INTEGER NOT NULL,
  created_at  REAL NOT NULL,
  PRIMARY KEY (job_id, key)
);
CREATE INDEX time_saved_day ON time_saved (day);
