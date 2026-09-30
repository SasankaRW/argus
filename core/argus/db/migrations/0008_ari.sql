-- Ari: conversations with Argus, and schedules you make by talking to it.

-- Schedules now have an owner: "config" (argus.yaml and plugin manifests, kept in step with them at start) or
-- "you" (made with Ari or in Helios; argusd never disables them on its own). A label says what it does.
ALTER TABLE schedules ADD COLUMN owner TEXT NOT NULL DEFAULT 'config';
ALTER TABLE schedules ADD COLUMN label TEXT;

-- One row per message. An answer from a model is filled in when its job finishes (job_id, text NULL until then).
-- `pending`: what Ari asked you to confirm (an action or a schedule), JSON; cleared once you answered.
CREATE TABLE ari_turns (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  conv        TEXT NOT NULL,
  role        TEXT NOT NULL CHECK (role IN ('you', 'ari')),
  text        TEXT,
  action      TEXT,
  pending     TEXT,
  job_id      TEXT,
  created_at  REAL NOT NULL
);
CREATE INDEX ari_turns_conv ON ari_turns (conv, id);
