-- Every model call (C0): which model, the prompt, the raw reply and the verdict, for Helios > Models > Calls and
-- each job's "Model calls". Pictures are never kept (only how many there were). Pruned by age and size.
CREATE TABLE model_calls (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    job_id     TEXT,
    plugin     TEXT,
    workflow   TEXT,
    step       TEXT,
    worker     TEXT,
    tier       TEXT,
    provider   TEXT,
    model      TEXT,
    attempt    INTEGER,
    verdict    TEXT NOT NULL,          -- ok, rejected, error
    reason     TEXT,                   -- why it was rejected, or the error
    latency_ms REAL,
    tokens_in  INTEGER,
    tokens_out INTEGER,
    images     INTEGER NOT NULL DEFAULT 0,
    system     TEXT,                   -- the playbook as sent (with lessons and examples)
    prompt     TEXT,                   -- the messages as sent (JSON), pictures left out
    reply      TEXT,                   -- the raw reply
    bytes      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX model_calls_job ON model_calls (job_id, id);
CREATE INDEX model_calls_time ON model_calls (created_at);
