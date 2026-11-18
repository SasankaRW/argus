"""The sender's disk queue, local history (last 200) and the outbox of events for Argus (send-queue.db).

Queue states: queued -> sending -> verifying -> done | held (handed to the holding agent) | failed | cancelled.
An entry is written before any byte is sent; `done` rows stay as history.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS queue (
  id TEXT PRIMARY KEY, target TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'send', state TEXT NOT NULL,
  reason TEXT, created REAL NOT NULL, updated REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
  next_try REAL NOT NULL DEFAULT 0, total INTEGER NOT NULL DEFAULT 0, sent INTEGER NOT NULL DEFAULT 0,
  origin TEXT, holder TEXT, route TEXT, asked INTEGER NOT NULL DEFAULT 0, manifest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS history (
  id INTEGER PRIMARY KEY AUTOINCREMENT, eid TEXT NOT NULL, at REAL NOT NULL, kind TEXT NOT NULL, peer TEXT,
  label TEXT, bytes INTEGER NOT NULL DEFAULT 0, detail TEXT, offer_id TEXT);
CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, body TEXT NOT NULL);
"""


class QueueStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=30, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)

    def _x(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, args)

    # ------------------------------------------------------------------ queue
    def sending(self) -> bool:
        """Something is mid-transfer (the agent waits for it before restarting on new code)."""
        return self._x("SELECT 1 FROM queue WHERE state IN ('sending','verifying') LIMIT 1").fetchone() is not None

    def add(self, target: str, manifest: list[dict], *, offer_id: str | None = None, kind: str = "send",
            origin: str | None = None, state: str = "queued") -> str:
        oid = offer_id or str(uuid.uuid4())
        now = time.time()
        total = sum(f["size"] for f in manifest)
        self._x("INSERT INTO queue(id,target,kind,state,created,updated,total,origin,manifest) "
                "VALUES(?,?,?,?,?,?,?,?,?)", (oid, target, kind, state, now, now, total, origin, json.dumps(manifest)))
        return oid

    def get(self, oid: str) -> dict | None:
        r = self._x("SELECT * FROM queue WHERE id=?", (oid,)).fetchone()
        return self._row(r) if r else None

    @staticmethod
    def _row(r: sqlite3.Row) -> dict:
        d = dict(r)
        d["manifest"] = json.loads(d["manifest"])
        return d

    def due(self, now: float) -> list[dict]:
        rows = self._x("SELECT * FROM queue WHERE state IN ('queued','held') AND next_try<=? ORDER BY created",
                       (now,)).fetchall()
        return [self._row(r) for r in rows]

    def active(self) -> list[dict]:
        rows = self._x("SELECT * FROM queue WHERE state IN ('queued','sending','verifying','held') ORDER BY created")
        return [self._row(r) for r in rows]

    def all(self, limit: int = 100) -> list[dict]:
        return [self._row(r) for r in self._x("SELECT * FROM queue ORDER BY created DESC LIMIT ?", (limit,))]

    def set(self, oid: str, **fields) -> None:
        if "manifest" in fields:
            fields["manifest"] = json.dumps(fields["manifest"])
        fields["updated"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        self._x(f"UPDATE queue SET {cols} WHERE id=?", (*fields.values(), oid))

    def kick(self) -> None:
        """A retry trigger: everything waiting may be tried now."""
        self._x("UPDATE queue SET next_try=0 WHERE state IN ('queued','held')")

    def reset_interrupted(self) -> None:
        """After a crash: what was sending or verifying goes back to the queue (the receiver keeps its chunks)."""
        self._x("UPDATE queue SET state='queued' WHERE state IN ('sending','verifying')")

    # ------------------------------------------------------------------ history and outbox
    def event(self, kind: str, *, peer: str | None = None, label: str | None = None, nbytes: int = 0,
              detail: str | None = None, offer_id: str | None = None) -> dict:
        ev = {"eid": uuid.uuid4().hex, "at": time.time(), "kind": kind, "peer": peer, "label": label,
              "bytes": nbytes, "detail": detail, "offer_id": offer_id}
        with self._lock:
            self._x("INSERT INTO history(eid,at,kind,peer,label,bytes,detail,offer_id) VALUES(?,?,?,?,?,?,?,?)",
                    (ev["eid"], ev["at"], kind, peer, label, nbytes, detail, offer_id))
            self._x("DELETE FROM history WHERE id <= (SELECT MAX(id) FROM history) - 200")
            self._x("INSERT INTO outbox(body) VALUES(?)", (json.dumps(ev),))
        return ev

    def history(self, limit: int = 200) -> list[dict]:
        return [dict(r) for r in self._x("SELECT * FROM history ORDER BY id DESC LIMIT ?", (limit,))]

    def outbox(self, limit: int = 50) -> list[tuple[int, dict]]:
        return [(r["id"], json.loads(r["body"])) for r in self._x("SELECT * FROM outbox ORDER BY id LIMIT ?", (limit,))]

    def outbox_done(self, ids: list[int]) -> None:
        for i in ids:
            self._x("DELETE FROM outbox WHERE id=?", (i,))

    def close(self) -> None:
        with self._lock:
            self._db.close()
