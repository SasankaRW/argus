"""Controls: stop, start, restart and pause what Argus runs, from Helios. See docs/controls.md.

Plugins are paused at runtime (a row in plugin_control): the job store neither hands out a paused plugin's queued
jobs nor queues new ones (schedules, triggers, webhooks and the API all go through `enqueue_in`), and the job a
worker already runs finishes. Workers are paused the same way (worker_control): their claim returns nothing while
they keep heartbeating. A restart asks the worker to finish its job and exit with EXIT_RESTART, so its supervisor
(or systemd) starts it again. Everything lives in the database, so it survives an Argus restart, and plugins.off in
argus.yaml is untouched: a plugin that is off cannot be started here.

The core (Argus itself) has no control here at all: its id is refused everywhere, and "pause everything" only
touches plugins and workers, never the API, Helios, the backups or approvals.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from typing import Any

from .db import Store
from .events import insert_event
from .exitcodes import EXIT_RESTART
from .registry import _aliases, _decode_worker

CORE_IDS = frozenset({"argus", "argusd", "core"})
SOURCE = "helios"


class ControlError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def base_worker(worker_id: str) -> str:
    """The process behind a lane: "worker-pc-now" (the fast lane) is the process "worker-pc"."""
    return worker_id[:-4] if worker_id.endswith("-now") else worker_id


def _workers(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    return {r["id"]: _decode_worker(r) for r in conn.execute("SELECT * FROM workers")}


def _box(conn: sqlite3.Connection, worker_id: str) -> str:
    """The box on the map this worker id is drawn as (a fast lane or a session twin folds into its worker)."""
    return _aliases(_workers(conn)).get(worker_id, worker_id)


def paused_plugins(conn: sqlite3.Connection) -> dict[str, float | None]:
    return {r["plugin"]: r["since"] for r in conn.execute("SELECT plugin, since FROM plugin_control WHERE paused = 1")}


def paused_workers(conn: sqlite3.Connection) -> dict[str, float | None]:
    return {r["worker"]: r["since"] for r in conn.execute("SELECT worker, since FROM worker_control WHERE paused = 1")}


def plugin_generations(conn: sqlite3.Connection) -> dict[str, int]:
    return {r["plugin"]: r["generation"] for r in conn.execute("SELECT plugin, generation FROM plugin_control")}


class Controls:
    def __init__(self, store: Store, cfg: Any, plugin_host: Any, jobs: Any, clock: Callable[[], float] = time.time):
        self.store, self.cfg, self.plugin_host, self.jobs, self.clock = store, cfg, plugin_host, jobs, clock

    # ------------------------------------------------------------------ checks

    def refuse_core(self, cid: str) -> None:
        if cid.lower() in CORE_IDS:
            raise ControlError(403, "Argus itself cannot be stopped, paused or restarted from here")

    def plugin_or_error(self, pid: str, verb: str) -> None:
        self.refuse_core(pid)
        if pid in self.cfg.plugins.off:
            raise ControlError(409, f"{pid} is switched off in plugins.off in argus.yaml; that setting decides, so it "
                                    f"cannot be {verb} here (take it out of the list and restart Argus)")
        if pid not in self.plugin_host.plugins:
            raise ControlError(404, f"no plugin {pid}")

    # ------------------------------------------------------------------ reads

    async def state(self) -> dict[str, Any]:
        def fn(conn: sqlite3.Connection) -> dict[str, Any]:
            flag = conn.execute("SELECT value FROM control_flags WHERE key = 'paused_all'").fetchone()
            return {"plugins_paused": sorted(paused_plugins(conn)), "workers_paused": sorted(paused_workers(conn)),
                    "paused_all": bool(flag and flag["value"] == "1")}

        return await self.store.read(fn)

    async def worker_gate(self, worker_id: str) -> tuple[bool, bool]:
        """(paused, restart_pending) for a claiming worker (one cheap read per poll)."""
        def fn(conn: sqlite3.Connection) -> tuple[bool, bool]:
            row = conn.execute("SELECT paused, restart_pending FROM worker_control WHERE worker = ?",
                               (_box(conn, worker_id),)).fetchone()
            if row is None:
                return False, False
            pending = json.loads(row["restart_pending"]) if row["restart_pending"] else []
            return bool(row["paused"]), base_worker(worker_id) in pending

        return await self.store.read(fn)

    async def take_restart(self, worker_id: str) -> bool:
        """The worker is being told to restart: it is no longer pending. False when it was not pending."""
        def fn(conn: sqlite3.Connection) -> bool:
            box = _box(conn, worker_id)
            row = conn.execute("SELECT restart_pending FROM worker_control WHERE worker = ?", (box,)).fetchone()
            pending = json.loads(row["restart_pending"]) if row and row["restart_pending"] else []
            me = base_worker(worker_id)
            if me not in pending:
                return False
            pending.remove(me)
            now = self.clock()
            conn.execute("UPDATE worker_control SET restart_pending = ?, updated_at = ? WHERE worker = ?",
                         (json.dumps(pending) if pending else None, now, box))
            insert_event(conn, now, "worker.restarting", src=me, dst="argus",
                         data={"exit_code": EXIT_RESTART, "box": box})
            return True

        return await self.store.write(fn)

    # ------------------------------------------------------------------ plugins

    def _set_plugin(self, conn: sqlite3.Connection, pid: str, paused: bool, reason: str | None, by_all: bool,
                    bump: bool = False) -> bool:
        """Write a plugin's row. Returns True when its paused state changed."""
        now = self.clock()
        row = conn.execute("SELECT paused, since FROM plugin_control WHERE plugin = ?", (pid,)).fetchone()
        was = bool(row and row["paused"])
        since = row["since"] if was and paused else (now if paused else None)
        conn.execute(
            "INSERT INTO plugin_control (plugin, paused, since, reason, by_all, generation, updated_at)"
            " VALUES (?,?,?,?,?,?,?) ON CONFLICT(plugin) DO UPDATE SET paused = excluded.paused,"
            " since = excluded.since, reason = excluded.reason, by_all = excluded.by_all,"
            " generation = plugin_control.generation + ?, updated_at = excluded.updated_at",
            (pid, int(paused), since, reason if paused else None, int(by_all and paused), int(bump), now, int(bump)))
        return was != paused

    async def stop_plugin(self, pid: str, reason: str | None = None) -> dict[str, Any]:
        self.plugin_or_error(pid, "stopped")

        def fn(conn: sqlite3.Connection) -> dict[str, Any]:
            changed = self._set_plugin(conn, pid, True, reason, by_all=False)
            insert_event(conn, self.clock(), "control.paused", src=SOURCE, dst=pid,
                         data={"kind": "plugin", "reason": reason, "changed": changed})
            return {"plugin": pid, "paused": True, "changed": changed}

        return await self.store.write(fn)

    async def start_plugin(self, pid: str) -> dict[str, Any]:
        self.plugin_or_error(pid, "started")

        def fn(conn: sqlite3.Connection) -> dict[str, Any]:
            changed = self._set_plugin(conn, pid, False, None, by_all=False)
            insert_event(conn, self.clock(), "control.resumed", src=SOURCE, dst=pid,
                         data={"kind": "plugin", "changed": changed})
            return {"plugin": pid, "paused": False, "changed": changed}

        return await self.store.write(fn)

    async def restart_plugin(self, pid: str) -> dict[str, Any]:
        """Start (clears a pause), put the plugin's dead or expired leases back in the queue, and tell workers to
        load its code again before their next job of it (the generation goes up)."""
        self.plugin_or_error(pid, "restarted")
        offline = await self.store.read(
            lambda c: [r["id"] for r in c.execute("SELECT id FROM workers WHERE state != 'online'")])
        requeued = await self.jobs.expire_leases(pid, offline_workers=offline)

        def fn(conn: sqlite3.Connection) -> dict[str, Any]:
            self._set_plugin(conn, pid, False, None, by_all=False, bump=True)
            gen = conn.execute("SELECT generation FROM plugin_control WHERE plugin = ?", (pid,)).fetchone()[0]
            insert_event(conn, self.clock(), "control.restarted", src=SOURCE, dst=pid,
                         data={"kind": "plugin", "requeued": requeued, "generation": gen})
            return {"plugin": pid, "paused": False, "requeued": requeued, "generation": gen}

        return await self.store.write(fn)

    # ------------------------------------------------------------------ workers

    def _worker_box(self, conn: sqlite3.Connection, worker_id: str) -> str:
        self.refuse_core(worker_id)
        box = _box(conn, worker_id)
        self.refuse_core(box)
        if conn.execute("SELECT 1 FROM workers WHERE id = ?", (box,)).fetchone() is None:
            raise ControlError(404, f"no worker {worker_id}")
        return box

    def _set_worker(self, conn: sqlite3.Connection, box: str, paused: bool, reason: str | None, by_all: bool) -> bool:
        now = self.clock()
        row = conn.execute("SELECT paused, since FROM worker_control WHERE worker = ?", (box,)).fetchone()
        was = bool(row and row["paused"])
        since = row["since"] if was and paused else (now if paused else None)
        conn.execute(
            "INSERT INTO worker_control (worker, paused, since, reason, by_all, updated_at) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(worker) DO UPDATE SET paused = excluded.paused, since = excluded.since,"
            " reason = excluded.reason, by_all = excluded.by_all, updated_at = excluded.updated_at",
            (box, int(paused), since, reason if paused else None, int(by_all and paused), now))
        return was != paused

    async def pause_worker(self, worker_id: str, reason: str | None = None) -> dict[str, Any]:
        def fn(conn: sqlite3.Connection) -> dict[str, Any]:
            box = self._worker_box(conn, worker_id)
            changed = self._set_worker(conn, box, True, reason, by_all=False)
            insert_event(conn, self.clock(), "control.paused", src=SOURCE, dst=box,
                         data={"kind": "worker", "reason": reason, "changed": changed})
            return {"worker": box, "paused": True, "changed": changed}

        return await self.store.write(fn)

    async def resume_worker(self, worker_id: str) -> dict[str, Any]:
        def fn(conn: sqlite3.Connection) -> dict[str, Any]:
            box = self._worker_box(conn, worker_id)
            changed = self._set_worker(conn, box, False, None, by_all=False)
            insert_event(conn, self.clock(), "control.resumed", src=SOURCE, dst=box,
                         data={"kind": "worker", "changed": changed})
            return {"worker": box, "paused": False, "changed": changed}

        return await self.store.write(fn)

    async def restart_worker(self, worker_id: str) -> dict[str, Any]:
        """Flag the worker's processes (the box, its fast lane and session twin): each is told on its next claim to
        finish the job it has and exit with EXIT_RESTART. Nothing is ever killed mid-job."""
        def fn(conn: sqlite3.Connection) -> dict[str, Any]:
            box = self._worker_box(conn, worker_id)
            workers = _workers(conn)
            alias = _aliases(workers)
            procs = sorted({base_worker(w) for w, d in workers.items()
                            if d["state"] == "online" and alias.get(w, w) == box})
            if not procs:
                raise ControlError(409, f"{box} is offline: nothing to restart (its supervisor starts it)")
            now = self.clock()
            conn.execute("INSERT INTO worker_control (worker, restart_pending, updated_at) VALUES (?,?,?)"
                         " ON CONFLICT(worker) DO UPDATE SET restart_pending = excluded.restart_pending,"
                         " updated_at = excluded.updated_at", (box, json.dumps(procs), now))
            insert_event(conn, now, "control.restarted", src=SOURCE, dst=box,
                         data={"kind": "worker", "processes": procs, "exit_code": EXIT_RESTART})
            return {"worker": box, "restarting": procs, "exit_code": EXIT_RESTART}

        return await self.store.write(fn)

    # ------------------------------------------------------------------ everything

    async def pause_all(self) -> dict[str, Any]:
        """Pause every plugin and every worker that is not paused already (those stay the user's own). The core, the
        API, Helios, the backups and approvals are not touched."""
        def fn(conn: sqlite3.Connection) -> dict[str, Any]:
            now = self.clock()
            already_p, already_w = paused_plugins(conn), paused_workers(conn)
            plugins = [p for p in sorted(self.plugin_host.plugins) if p not in self.cfg.plugins.off
                       and p not in already_p]
            workers_all = _workers(conn)
            alias = _aliases(workers_all)
            workers = [w for w in sorted(workers_all) if w not in alias and w.lower() not in CORE_IDS
                       and w not in already_w]
            for pid in plugins:
                self._set_plugin(conn, pid, True, "pause everything", by_all=True)
                insert_event(conn, now, "control.paused", src=SOURCE, dst=pid,
                             data={"kind": "plugin", "reason": "pause everything", "changed": True})
            for w in workers:
                self._set_worker(conn, w, True, "pause everything", by_all=True)
                insert_event(conn, now, "control.paused", src=SOURCE, dst=w,
                             data={"kind": "worker", "reason": "pause everything", "changed": True})
            conn.execute("INSERT INTO control_flags (key, value, updated_at) VALUES ('paused_all', '1', ?)"
                         " ON CONFLICT(key) DO UPDATE SET value = '1', updated_at = excluded.updated_at", (now,))
            insert_event(conn, now, "control.paused", src=SOURCE, dst="argus",
                         data={"scope": "all", "plugins": plugins, "workers": workers})
            return {"paused_all": True, "plugins": plugins, "workers": workers}

        return await self.store.write(fn)

    async def resume_all(self) -> dict[str, Any]:
        """Put back exactly what pause-all paused; anything you paused yourself stays paused."""
        def fn(conn: sqlite3.Connection) -> dict[str, Any]:
            now = self.clock()
            plugins = [r[0] for r in conn.execute(
                "SELECT plugin FROM plugin_control WHERE paused = 1 AND by_all = 1 ORDER BY plugin")]
            workers = [r[0] for r in conn.execute(
                "SELECT worker FROM worker_control WHERE paused = 1 AND by_all = 1 ORDER BY worker")]
            for pid in plugins:
                self._set_plugin(conn, pid, False, None, by_all=False)
                insert_event(conn, now, "control.resumed", src=SOURCE, dst=pid,
                             data={"kind": "plugin", "changed": True, "by": "resume everything"})
            for w in workers:
                self._set_worker(conn, w, False, None, by_all=False)
                insert_event(conn, now, "control.resumed", src=SOURCE, dst=w,
                             data={"kind": "worker", "changed": True, "by": "resume everything"})
            conn.execute("INSERT INTO control_flags (key, value, updated_at) VALUES ('paused_all', '0', ?)"
                         " ON CONFLICT(key) DO UPDATE SET value = '0', updated_at = excluded.updated_at", (now,))
            insert_event(conn, now, "control.resumed", src=SOURCE, dst="argus",
                         data={"scope": "all", "plugins": plugins, "workers": workers})
            return {"paused_all": False, "plugins": plugins, "workers": workers}

        return await self.store.write(fn)
