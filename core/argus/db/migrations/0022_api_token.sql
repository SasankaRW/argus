-- Named API tokens you can revoke one by one (the phone, a script). Only the SHA-256 of a token is kept; the token
-- itself is shown once when it is made. The worker token in argus.yaml / .env keeps working next to these.
CREATE TABLE api_token (
  id           INTEGER PRIMARY KEY,
  name         TEXT NOT NULL UNIQUE,
  hash         TEXT NOT NULL,               -- sha256 hex of the token
  created_at   REAL NOT NULL,
  last_used_at REAL,
  revoked_at   REAL
);
