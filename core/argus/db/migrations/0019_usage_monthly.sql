-- Month-by-month use per plugin (F1). Raw events are pruned after events.retention_days, so their counts are
-- rolled up here first and kept much longer.
CREATE TABLE usage_monthly (
  month        TEXT NOT NULL,              -- 'YYYY-MM', local time
  plugin       TEXT NOT NULL,
  model_calls  INTEGER NOT NULL DEFAULT 0,
  escalations  INTEGER NOT NULL DEFAULT 0,
  job_runs     INTEGER NOT NULL DEFAULT 0, -- jobs finished (ok or not)
  job_failed   INTEGER NOT NULL DEFAULT 0, -- of those, dead or cancelled
  PRIMARY KEY (month, plugin)
);
