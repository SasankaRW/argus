-- Argus Send: paired devices and the shared history the agents report (the agents work without Argus; this is
-- the helper's copy). Addresses are re-registered by each agent every 30 s.
CREATE TABLE send_device (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  pubkey      TEXT NOT NULL,
  lan         TEXT NOT NULL DEFAULT '[]',   -- JSON list of ip:port
  tailscale   TEXT,
  seen_at     REAL NOT NULL
);

CREATE TABLE send_history (
  eid         TEXT PRIMARY KEY,             -- the agent's event id, so a re-sent event is stored once
  at          REAL NOT NULL,
  device      TEXT NOT NULL,                -- the agent that reported it
  kind        TEXT NOT NULL,                -- sent, received, held, delivered, failed, ...
  peer        TEXT,
  label       TEXT,
  bytes       INTEGER NOT NULL DEFAULT 0,
  detail      TEXT,
  offer_id    TEXT
);
CREATE INDEX send_history_at ON send_history(at DESC);
