"""Registry: which workers are connected and which components exist.

The Helios live map draws its boxes from `components`; workers also land in `workers` with their capabilities.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Iterable
from typing import Any

from .db import Store
from .ids import new_id

COMPONENT_KINDS = {"core", "plugin", "model", "app", "worker", "service"}


class Registry:
    def __init__(self, store: Store, clock: Callable[[], float] = time.time):
        self.store = store
        self.clock = clock

    @staticmethod
    def _upsert_component(conn: sqlite3.Connection, now: float, cid: str, kind: str, label: str,
                          group: str | None, meta: dict[str, Any] | None) -> bool:
        if kind not in COMPONENT_KINDS:
            raise ValueError(f"unknown component kind {kind!r}")
        existed = conn.execute("SELECT 1 FROM components WHERE id = ?", (cid,)).fetchone() is not None
        conn.execute(
            "INSERT INTO components (id, kind, label, grp, meta, first_seen, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET kind=excluded.kind, label=excluded.label,"
            " grp=excluded.grp, meta=excluded.meta, updated_at=excluded.updated_at",
            (cid, kind, label, group, json.dumps(meta or {}), now, now, now),
        )
        return not existed

    async def component(self, cid: str, kind: str, label: str, group: str | None = None,
                        meta: dict[str, Any] | None = None) -> bool:
        """Add or update a component. Returns True the first time it is seen."""

        def fn(conn: sqlite3.Connection) -> bool:
            now = self.clock()
            new = self._upsert_component(conn, now, cid, kind, label, group, meta)
            if new:
                conn.execute(
                    "INSERT INTO events (id, kind, from_component, to_component, data, at, created_at, updated_at)"
                    " VALUES (?, 'component.added', ?, NULL, ?, ?, ?, ?)",
                    (new_id(), cid, json.dumps({"kind": kind, "label": label}), now, now, now),
                )
            return new

        return await self.store.write(fn)

    async def register_worker(self, worker_id: str, host: str, capabilities: Iterable[str],
                              version: str | None = None) -> None:
        caps = sorted(set(capabilities))

        def fn(conn: sqlite3.Connection) -> None:
            now = self.clock()
            conn.execute(
                "INSERT INTO workers (id, host, capabilities, version, last_seen, state, created_at, updated_at)"
                " VALUES (?,?,?,?,?, 'online', ?, ?) ON CONFLICT(id) DO UPDATE SET host=excluded.host,"
                " capabilities=excluded.capabilities, version=excluded.version, last_seen=excluded.last_seen,"
                " state='online', updated_at=excluded.updated_at",
                (worker_id, host, json.dumps(caps), version, now, now, now),
            )
            self._upsert_component(conn, now, worker_id, "worker", worker_id, host,
                                   {"capabilities": caps, "version": version})

        await self.store.write(fn)

    async def touch_worker(self, worker_id: str) -> None:
        def fn(conn: sqlite3.Connection) -> None:
            now = self.clock()
            conn.execute("UPDATE workers SET last_seen = ?, state = 'online', updated_at = ? WHERE id = ?",
                         (now, now, worker_id))

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
            return ids

        return await self.store.write(fn)

    async def workers(self) -> list[dict[str, Any]]:
        def fn(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            out = []
            for r in conn.execute("SELECT * FROM workers ORDER BY id").fetchall():
                d = dict(r)
                d["capabilities"] = json.loads(d["capabilities"])
                out.append(d)
            return out

        return await self.store.read(fn)

    async def components(self) -> list[dict[str, Any]]:
        def fn(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            out = []
            for r in conn.execute("SELECT * FROM components ORDER BY kind, id").fetchall():
                d = dict(r)
                d["meta"] = json.loads(d["meta"])
                d["group"] = d.pop("grp")
                out.append(d)
            return out

        return await self.store.read(fn)
