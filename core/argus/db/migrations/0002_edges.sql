-- Edges: which components have talked to each other, so the Helios map grows as Argus is used.
-- Kept forever (tiny); events themselves are pruned after the retention period.
CREATE TABLE edges (
  src         TEXT NOT NULL,
  dst         TEXT NOT NULL,
  count       INTEGER NOT NULL DEFAULT 0,
  last_kind   TEXT,
  first_seen  REAL NOT NULL,
  last_seen   REAL NOT NULL,
  PRIMARY KEY (src, dst)
);
