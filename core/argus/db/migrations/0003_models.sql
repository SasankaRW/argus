-- Model tiers: circuit breaker state and call counters, shared by every worker.
CREATE TABLE model_state (
  tier                  TEXT PRIMARY KEY,
  state                 TEXT NOT NULL DEFAULT 'closed',   -- closed | open | half_open
  consecutive_failures  INTEGER NOT NULL DEFAULT 0,
  opened_until          REAL,
  last_error            TEXT,
  calls                 INTEGER NOT NULL DEFAULT 0,
  failures              INTEGER NOT NULL DEFAULT 0,
  last_latency_ms       REAL,
  created_at            REAL NOT NULL,
  updated_at            REAL NOT NULL
);

-- Daily budgets (Claude calls per day). One row per day and key.
CREATE TABLE budget (
  day         TEXT NOT NULL,
  key         TEXT NOT NULL,
  used        INTEGER NOT NULL DEFAULT 0,
  created_at  REAL NOT NULL,
  updated_at  REAL NOT NULL,
  PRIMARY KEY (day, key)
);
