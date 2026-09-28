"""Registry: which components exist, which workers are connected, and who talks to whom.

Helios draws its map from here: `components` are the boxes, `edges` are the lines (they appear the first
time two components exchange an event), and worker online/offline changes arrive as events.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Iterable
from typing import Any

from .db import Store
from .events import insert_event, last_seq

COMPONENT_KINDS = {"core", "plugin", "model", "app", "worker", "service"}


def _upsert_component(conn: sqlite3.Connection, now: float, cid: str, kind: str, label: str,
                      group: str | None, meta: dict[str, Any] | None) -> bool:
    """Insert or update a component. Returns True (and writes component.added) the first time."""
    if kind not in COMPONENT_KINDS:
        raise ValueError(f"unknown component kind {kind!r}")
    existed = conn.execute("SELECT 1 FROM components WHERE id = ?", (cid,)).fetchone() is not None
    conn.execute(
        "INSERT INTO components (id, kind, label, grp, meta, first_seen, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET kind=excluded.kind, label=excluded.label,"
        " grp=excluded.grp, meta=excluded.meta, updated_at=excluded.updated_at",
        (cid, kind, label, group, json.dumps(meta or {}), now, now, now),
    )
    if not existed:
        insert_event(conn, now, "component.added", src=cid, data={"kind": kind, "label": label, "group": group})
    return not existed


def ensure_component(conn: sqlite3.Connection, now: float, cid: str, kind: str, label: str,
                     group: str | None = None) -> bool:
    """Add a component if it is new; leave an existing one untouched. Call inside a Store write."""
    if conn.execute("SELECT 1 FROM components WHERE id = ?", (cid,)).fetchone() is not None:
        return False
    return _upsert_component(conn, now, cid, kind, label, group, None)


def _decode_component(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    d["meta"] = json.loads(d["meta"]) if d["meta"] else {}
    d["group"] = d.pop("grp")
    return d


def _decode_worker(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    d["capabilities"] = json.loads(d["capabilities"])
    return d


class Registry:
    def __init__(self, store: Store, clock: Callable[[], float] = time.time):
        self.store = store
        self.clock = clock

    async def component(self, cid: str, kind: str, label: str, group: str | None = None,
                        meta: dict[str, Any] | None = None) -> bool:
        """Add or update a component. Returns True the first time it is seen."""
        return await self.store.write(lambda conn: _upsert_component(conn, self.clock(), cid, kind, label,
                                                                     group, meta))

    # -------------------------------------------------------------- workers

    async def register_worker(self, worker_id: str, host: str, capabilities: Iterable[str],
                              version: str | None = None) -> None:
        caps = sorted(set(capabilities))

        def fn(conn: sqlite3.Connection) -> None:
            now = self.clock()
            row = conn.execute("SELECT state FROM workers WHERE id = ?", (worker_id,)).fetchone()
            conn.execute(
                "INSERT INTO workers (id, host, capabilities, version, last_seen, state, created_at, updated_at)"
                " VALUES (?,?,?,?,?, 'online', ?, ?) ON CONFLICT(id) DO UPDATE SET host=excluded.host,"
                " capabilities=excluded.capabilities, version=excluded.version, last_seen=excluded.last_seen,"
                " state='online', updated_at=excluded.updated_at",
                (worker_id, host, json.dumps(caps), version, now, now, now),
            )
            _upsert_component(conn, now, worker_id, "worker", worker_id, host,
                              {"capabilities": caps, "version": version})
            insert_event(conn, now, "worker.online", src=worker_id,
                         data={"host": host, "capabilities": caps, "version": version,
                               "was": row["state"] if row else None})

        await self.store.write(fn)

    async def touch_worker(self, worker_id: str) -> None:
        def fn(conn: sqlite3.Connection) -> None:
            now = self.clock()
            row = conn.execute("SELECT state FROM workers WHERE id = ?", (worker_id,)).fetchone()
            if row is None:
                return
            conn.execute("UPDATE workers SET last_seen = ?, state = 'online', updated_at = ? WHERE id = ?",
                         (now, now, worker_id))
            if row["state"] != "online":
                insert_event(conn, now, "worker.online", src=worker_id, data={"was": row["state"]})

        await self.store.write(fn)

    async def mark_stale_workers(self, older_than: float) -> list[str]:
        def fn(conn: sqlite3.Connection) -> list[str]:
            now = self.clock()
            rows = conn.execute(
                "SELECT id FROM workers WHERE state = 'online' AND last_seen < ?", (now - older_than,)
            ).fetchall()
            ids = [r[0] for r in rows]
            for wid in ids:
                conn.execute("UPDATE workers SET state = 'offline', updated_at = ? WHERE id = ?", (now, wid))
                insert_event(conn, now, "worker.offline", src=wid, data={"silent_seconds": older_than})
            return ids

        return await self.store.write(fn)

    # -------------------------------------------------------------- reads

    async def workers(self) -> list[dict[str, Any]]:
        return await self.store.read(
            lambda c: [_decode_worker(r) for r in c.execute("SELECT * FROM workers ORDER BY id").fetchall()])

    async def components(self) -> list[dict[str, Any]]:
        return await self.store.read(
            lambda c: [_decode_component(r)
                       for r in c.execute("SELECT * FROM components ORDER BY kind, id").fetchall()])

    async def edges(self) -> list[dict[str, Any]]:
        return await self.store.read(
            lambda c: [dict(r) for r in c.execute("SELECT * FROM edges ORDER BY src, dst").fetchall()])

    async def map(self) -> dict[str, Any]:
        """Everything Helios needs to draw the map, read in one consistent snapshot, plus the event `seq`
        to stream from so no change between the snapshot and the stream is missed."""

        def fn(conn: sqlite3.Connection) -> dict[str, Any]:
            conn.execute("BEGIN")
            try:
                seq = last_seq(conn)
                comps = [_decode_component(r)
                         for r in conn.execute("SELECT * FROM components ORDER BY kind, id").fetchall()]
                workers = {r["id"]: _decode_worker(r) for r in conn.execute("SELECT * FROM workers").fetchall()}
                edges = [dict(r) for r in conn.execute("SELECT * FROM edges ORDER BY src, dst").fetchall()]
                active = {r[0]: r[1] for r in conn.execute(
                    "SELECT plugin, COUNT(*) FROM jobs WHERE state IN ('leased','running','waiting')"
                    " GROUP BY plugin")}
                queued = {r[0]: r[1] for r in conn.execute(
                    "SELECT plugin, COUNT(*) FROM jobs WHERE state IN ('queued','retry') GROUP BY plugin")}
            finally:
                conn.execute("COMMIT")
            known = {c["id"] for c in comps}
            nodes = []
            for c in comps:
                node = {"id": c["id"], "kind": c["kind"], "label": c["label"], "group": c["group"],
                        "meta": c["meta"], "first_seen": c["first_seen"]}
                if c["kind"] == "worker" and c["id"] in workers:
                    node["state"] = workers[c["id"]]["state"]
                if c["kind"] == "plugin":
                    node["jobs"] = {"active": active.get(c["id"], 0), "queued": queued.get(c["id"], 0)}
                nodes.append(node)
            # endpoints seen in events but never registered (e.g. the watchdog) still get a box
            for e in edges:
                for end in (e["src"], e["dst"]):
                    if end not in known:
                        known.add(end)
                        nodes.append({"id": end, "kind": "service", "label": end, "group": None, "meta": {},
                                      "first_seen": e["first_seen"], "implicit": True})
            return {"seq": seq, "nodes": nodes, "edges": edges}

        return await self.store.read(fn)
