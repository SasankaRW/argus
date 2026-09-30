-- Things you told Ari to remember ("my car service is due in December"). Searched for every question Ari thinks
-- about, so it answers from what you told it first. Only what you asked it to keep.
CREATE TABLE ari_memory (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  fact        TEXT NOT NULL,
  created_at  REAL NOT NULL,
  updated_at  REAL NOT NULL
);
CREATE VIRTUAL TABLE ari_memory_fts USING fts5(fact, content='ari_memory', content_rowid='id',
                                              tokenize='porter unicode61');
CREATE TRIGGER ari_memory_ai AFTER INSERT ON ari_memory BEGIN
  INSERT INTO ari_memory_fts(rowid, fact) VALUES (new.id, new.fact);
END;
CREATE TRIGGER ari_memory_ad AFTER DELETE ON ari_memory BEGIN
  INSERT INTO ari_memory_fts(ari_memory_fts, rowid, fact) VALUES ('delete', old.id, old.fact);
END;
CREATE TRIGGER ari_memory_au AFTER UPDATE ON ari_memory BEGIN
  INSERT INTO ari_memory_fts(ari_memory_fts, rowid, fact) VALUES ('delete', old.id, old.fact);
  INSERT INTO ari_memory_fts(rowid, fact) VALUES (new.id, new.fact);
END;

-- Which tools Ari used for an answer ("opened Spotify, set the volume"), shown under its reply in Helios.
ALTER TABLE ari_turns ADD COLUMN used TEXT;
