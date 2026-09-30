"""The scheduler: cron schedules become jobs at the right time.

Schedules are defined in argus.yaml (and, from C10, in plugin manifests). The `schedules` table only keeps their
clock: when each runs next and what it last queued. Every watchdog tick, due schedules are enqueued, each in one
transaction with its own clock update, so a crash can never queue a run twice or skip one.

If Argus was down when a schedule was due, it runs once when Argus is back (not once per missed slot), then
follows its cron again. A run that is still queued or running when the next one is due is merged (dedupe key).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Callable
from typing import Any

from .config import Config, ScheduleConfig
from .cron import next_run
from .db import Store
from .events import insert_event
from .jobs.store import JobStore, QueueFull
from .outbox import add_message, ntfy_message
from .registry import ensure_component

log = logging.getLogger("argus.scheduler")


def _spec(s: ScheduleConfig) -> dict[str, Any]:
    return {"input": s.input, "needs": s.needs, "priority": s.priority, "model": s.model, "window": s.window}


class Scheduler:
    def __init__(self, store: Store, jobs: JobStore, cfg: Config, clock: Callable[[], float] = time.time):
        self.store = store
        self.jobs = jobs
        self.cfg = cfg
        self.clock = clock
        self.queued = 0
        self.poke: Callable[[], None] = lambda: None  # wakes the outbox (set by Argus) after a reminder

    async def sync(self, schedules: list[ScheduleConfig] | None = None) -> None:
        """Make the table match the config: add new schedules, update changed ones, disable removed ones."""
        items = self.cfg.schedules if schedules is None else schedules

        def fn(conn: sqlite3.Connection) -> None:
            now = self.clock()
            if items:
                ensure_component(conn, now, "scheduler", "service", "Scheduler", None)
            known = {r["id"]: r for r in conn.execute("SELECT * FROM schedules WHERE owner = 'config'").fetchall()}
            for s in items:
                spec = json.dumps(_spec(s), sort_keys=True)
                row = known.pop(s.id, None)
                if row is None:
                    conn.execute(
                        "INSERT INTO schedules (id, plugin, workflow, cron, spec, enabled, next_run_at, created_at,"
                        " updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                        (s.id, s.plugin, s.workflow, s.cron, spec, int(s.enabled), next_run(s.cron, now), now, now))
                    continue
                # a new cron restarts the clock; anything else keeps it
                nxt = row["next_run_at"] if row["cron"] == s.cron and row["next_run_at"] else next_run(s.cron, now)
                conn.execute(
                    "UPDATE schedules SET plugin = ?, workflow = ?, cron = ?, spec = ?, enabled = ?, next_run_at = ?,"
                    " updated_at = ? WHERE id = ?",
                    (s.plugin, s.workflow, s.cron, spec, int(s.enabled), nxt, now, s.id))
            for gone in known:  # removed from the config: keep the history, stop running it
                conn.execute("UPDATE schedules SET enabled = 0, updated_at = ? WHERE id = ?", (now, gone))

        await self.store.write(fn)

    async def tick(self) -> int:
        """Queue every due schedule. Returns how many jobs were queued."""

        def due(conn: sqlite3.Connection) -> list[str]:
            return [r[0] for r in conn.execute(
                "SELECT id FROM schedules WHERE enabled = 1 AND next_run_at <= ? ORDER BY next_run_at",
                (self.clock(),))]

        queued = 0
        for sid in await self.store.read(due):
            try:
                if await self.store.write(lambda conn, sid=sid: self._run(conn, sid)):
                    queued += 1
            except QueueFull as e:
                log.warning("schedule skipped: plugin queue full", extra={"schedule": sid, "error": str(e)})
                await self.store.write(lambda conn, sid=sid: self._advance(conn, sid, None))
        self.queued += queued
        return queued

    def _run(self, conn: sqlite3.Connection, sid: str, *, now_run: bool = False) -> bool:
        now = self.clock()
        row = conn.execute("SELECT * FROM schedules WHERE id = ?", (sid,)).fetchone()
        if row is None or (not now_run and (not row["enabled"] or row["next_run_at"] > now)):
            return False
        spec = json.loads(row["spec"])
        slot = int(now) if now_run else int(row["next_run_at"])
        if row["plugin"] == "argus" and row["workflow"] == "remind":  # a reminder: straight to the phone, no job
            text = str(spec.get("input", {}).get("text") or row["label"] or "Reminder")
            add_message(conn, now, "ntfy", ntfy_message("Reminder", text[:500], priority="high", tags=["bell"]),
                        dedupe_key=f"remind:{sid}:{slot}")
            insert_event(conn, now, "schedule.reminded", src="scheduler", dst="phone", data={"schedule": sid})
            self._advance(conn, sid, None, now_run=now_run, once=bool(spec.get("once")))
            self.poke()
            return True
        job_id, created = self.jobs.enqueue_in(
            conn, now, row["plugin"], row["workflow"], {**spec.get("input", {}), "_schedule": sid, "_slot": slot},
            needs=spec.get("needs", []), priority=spec.get("priority", 50), model_group=spec.get("model"),
            window=spec.get("window"), dedupe_key=f"schedule:{sid}", source="scheduler")
        self._advance(conn, sid, job_id, now_run=now_run, once=bool(spec.get("once")))
        return created

    def _advance(self, conn: sqlite3.Connection, sid: str, job_id: str | None, *, now_run: bool = False,
                 once: bool = False) -> None:
        now = self.clock()
        if once and not now_run:  # a one-off ("tomorrow at 7"): done after its run
            conn.execute("UPDATE schedules SET last_run_at = ?, last_job_id = COALESCE(?, last_job_id), enabled = 0,"
                         " next_run_at = NULL, updated_at = ? WHERE id = ?", (now, job_id, now, sid))
            return
        row = conn.execute("SELECT cron FROM schedules WHERE id = ?", (sid,)).fetchone()
        if now_run:  # "run now" does not move the regular clock
            conn.execute("UPDATE schedules SET last_run_at = ?, last_job_id = ?, updated_at = ? WHERE id = ?",
                         (now, job_id, now, sid))
            return
        # catch up once: the next slot after now, not after the missed one
        conn.execute("UPDATE schedules SET last_run_at = ?, last_job_id = COALESCE(?, last_job_id),"
                     " next_run_at = ?, updated_at = ? WHERE id = ?",
                     (now, job_id, next_run(row["cron"], now), now, sid))

    async def run_now(self, sid: str) -> str | None:
        """The Run now button: queue it at once (merged with a run already queued)."""

        def fn(conn: sqlite3.Connection) -> str | None:
            if conn.execute("SELECT 1 FROM schedules WHERE id = ?", (sid,)).fetchone() is None:
                return None
            self._run(conn, sid, now_run=True)
            return conn.execute("SELECT last_job_id FROM schedules WHERE id = ?", (sid,)).fetchone()[0]

        return await self.store.write(fn)

    async def list(self) -> list[dict[str, Any]]:
        def fn(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            out = []
            for r in conn.execute("SELECT * FROM schedules ORDER BY id").fetchall():
                d = dict(r)
                d["spec"] = json.loads(d["spec"])
                d["enabled"] = bool(d["enabled"])
                out.append(d)
            return out

        return await self.store.read(fn)
