-- What happened in a chat, kept short (Ari's memory over time).
-- A running summary of a chat's turns before the last few (the model sees the last turns word for word and this
-- for everything earlier that day), and an episode per finished chat: 2-3 lines on what you asked, what worked,
-- what didn't and what was left open, searchable across days. Chats where something private was used (the screen,
-- the clipboard, mail, money) get neither.
CREATE TABLE ari_summaries (
  conv        TEXT PRIMARY KEY,
  summary     TEXT NOT NULL,
  upto        INTEGER NOT NULL,          -- the last turn id it covers
  updated_at  REAL NOT NULL
);
CREATE TABLE ari_episodes (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  conv        TEXT NOT NULL UNIQUE,
  text        TEXT NOT NULL,
  left_open   TEXT,
  started_at  REAL NOT NULL,
  ended_at    REAL NOT NULL,             -- the chat's last turn when the episode was written
  created_at  REAL NOT NULL
);
CREATE INDEX ari_episodes_ended ON ari_episodes (ended_at);
CREATE VIRTUAL TABLE ari_episodes_fts USING fts5(text, left_open, content='ari_episodes', content_rowid='id',
                                                 tokenize='porter unicode61');
CREATE TRIGGER ari_episodes_ai AFTER INSERT ON ari_episodes BEGIN
  INSERT INTO ari_episodes_fts(rowid, text, left_open) VALUES (new.id, new.text, new.left_open);
END;
CREATE TRIGGER ari_episodes_ad AFTER DELETE ON ari_episodes BEGIN
  INSERT INTO ari_episodes_fts(ari_episodes_fts, rowid, text, left_open) VALUES ('delete', old.id, old.text,
                                                                                  old.left_open);
END;
CREATE TRIGGER ari_episodes_au AFTER UPDATE ON ari_episodes BEGIN
  INSERT INTO ari_episodes_fts(ari_episodes_fts, rowid, text, left_open) VALUES ('delete', old.id, old.text,
                                                                                  old.left_open);
  INSERT INTO ari_episodes_fts(rowid, text, left_open) VALUES (new.id, new.text, new.left_open);
END;
