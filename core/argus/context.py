"""The running Argus instance: config, store, jobs and background tasks, started and stopped together."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

from . import __version__
from .approvals import Approvals
from .backup import Backups
from .config import PRIORITY_BATCH, Config, ScheduleConfig
from .daily import Marker, brief_due, compose_brief, last_health, resume_summary
from .db import Store
from .events import EventHub, insert_event, prune_events
from .jobs import JobStore, Watchdog
from .modelboard import ModelBoard
from .outbox import Outbox, add_message, ntfy_message
from .plugins import PluginHost
from .power import PowerManager
from .presence import PhoneWatch
from .registry import Registry
from .relay import ReplyRelay
from .scheduler import Scheduler
from .triggers import Triggers

log = logging.getLogger("argus")


class Argus:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.version = __version__
        self.store = Store(cfg.db_path)
        self.jobs = JobStore(self.store, cfg.jobs, windows=cfg.windows)
        self.registry = Registry(self.store)
        self.models = ModelBoard(self.store, cfg)
        self.outbox = Outbox(self.store, cfg)
        self.approvals = Approvals(self.store, self.jobs, cfg)
        self.relay = ReplyRelay(self.store, cfg, self.approvals, self.outbox)
        self.phone = PhoneWatch(self.store, cfg, self.approvals, self.outbox)
        self.plugin_host = PluginHost(cfg)
        self.power = PowerManager(self.store, cfg)
        self.power.jobs = self.jobs
        self.scheduler = Scheduler(self.store, self.jobs, cfg)
        self.scheduler.poke = self.outbox.poke
        self.triggers = Triggers(self.store, self.jobs, cfg)
        self.hub = EventHub(self.store, queue_size=cfg.events.stream_queue)
        stale_after = cfg.jobs.heartbeat_seconds * 4
        self.watchdog = Watchdog(
            self.jobs,
            cfg.jobs.watchdog_interval_seconds,
            extra=[lambda: self.registry.mark_stale_workers(stale_after), self.prune_events,
                   self.approval_tick, self.scheduler.tick,
                   self.power.tick, self.backup_tick, self.daily_tick],
        )
        self.backups = Backups(cfg.db_path, cfg.backup.keep)
        self.last_backup: dict | None = None
        self.marker = Marker(cfg.db_path.parent / "argus.running")
        self.brief_sent: str | None = None
        self._next_touch = 0.0
        self.resumed: dict | None = None  # set at start when the last run ended abruptly
        self.started_at: float | None = None
        self._next_prune = 0.0
        self.pruned_events = 0

    def open(self) -> Argus:
        """Synchronous part of startup: open the database and run migrations."""
        self.store.open()
        return self

    async def start(self) -> None:
        """Async part of startup: background tasks."""
        group = "laptop" if self.cfg.instance.host == "laptop" else "pc"
        await self.registry.component("argus", "core", "Argus", group, {"version": self.version})
        await self.models.register()
        await self.approvals.start()
        stopped_at = self.marker.start()
        if stopped_at is not None:  # the last run ended abruptly (power cut, crash): say what happens now
            await self.resume_notice(stopped_at)
        self.plugin_host.load()  # adds the plugins' schedules and triggers before the scheduler reads them
        self._health_schedule()
        await self.register_plugins()
        self.models.plugin_caps = self.plugin_host.claude_caps()
        await self.scheduler.sync()
        await self.power.start()
        await self.outbox.start()
        self.relay.start()
        await self.phone.start()
        await self.hub.start()
        self.watchdog.start()
        self.started_at = time.time()
        log.info("argus started", extra={"version": self.version, "instance": self.cfg.instance.name})

    def _health_schedule(self) -> None:
        """health.enabled: the PC's worker checks WSL and Docker every health.every_minutes."""
        h = self.cfg.health
        if not h.enabled or any(s.id == "argus-health" for s in self.cfg.schedules):
            return
        n = h.every_minutes
        cron = f"*/{n} * * * *" if n < 60 else f"0 */{max(1, n // 60)} * * *"
        self.cfg.schedules.append(ScheduleConfig(id="argus-health", plugin="health", workflow="check", cron=cron,
                                                 input={"containers": h.containers}, needs=["desktop"],
                                                 priority=PRIORITY_BATCH))

    async def resume_notice(self, stopped_at: float) -> None:
        def fn(conn):
            now = time.time()
            title, text, data = resume_summary(conn, now, stopped_at)
            insert_event(conn, now, "argus.resumed", src="argus", dst="phone", data=data)
            add_message(conn, now, "ntfy", ntfy_message(title, text, tags=["electric_plug"]),
                        dedupe_key=f"resumed:{int(stopped_at)}")
            return data

        self.resumed = await self.store.write(fn)
        log.warning("argus did not stop cleanly last time", extra=self.resumed)

    async def daily_tick(self) -> None:
        now = time.time()
        if now >= self._next_touch:
            self._next_touch = now + 60
            await asyncio.to_thread(self.marker.touch)
        if self.cfg.brief.enabled:
            day = brief_due(self.cfg.brief.at, now, self.brief_sent)
            if day:
                await self.send_brief(day)

    async def send_brief(self, day: str | None = None) -> dict:
        """The morning brief to the phone (once per day; `POST /brief` sends one now)."""
        needs = set(self.cfg.power.pc_needs)
        pc = any(w["state"] == "online" and set(w["capabilities"]) & needs for w in await self.registry.workers())

        def fn(conn):
            now = time.time()
            title, text = compose_brief(conn, now, last_backup=self.last_backup, pc_online=pc,
                                        health=last_health(conn), claude_cap=self.cfg.claude.calls_per_day)
            key = f"brief:{day}" if day else f"brief:now:{int(now)}"
            oid = add_message(conn, now, "ntfy", ntfy_message(title, text, tags=["sunrise"]), dedupe_key=key)
            return {"title": title, "text": text, "queued": oid is not None}

        out = await self.store.write(fn)
        if day:
            self.brief_sent = day
        self.outbox.poke()
        return out

    def _backup_due(self, now: float) -> bool:
        """Tonight's slot has passed and the newest backup is older than it."""
        if not self.cfg.backup.enabled:
            return False
        h, m = map(int, self.cfg.backup.at.split(":"))
        slot = datetime.fromtimestamp(now).replace(hour=h, minute=m, second=0, microsecond=0).timestamp()
        if now < slot:
            slot -= 86400
        if self.last_backup and self.last_backup.get("at", 0) >= slot:
            return False  # tried for this slot already (a failed one is not retried until the next)
        latest = self.backups.list()
        return not latest or latest[0]["made_at"] < slot

    async def backup_tick(self) -> None:
        if self._backup_due(time.time()):
            await self.backup_now()

    async def backup_now(self) -> dict:
        """Back up and check it; tell the phone if it failed; have the PC fetch a copy (backup.copy_to)."""
        try:
            res = await asyncio.to_thread(self.backups.make)
        except Exception as e:  # disk full, database locked for too long, ...
            res = {"ok": False, "problem": f"{type(e).__name__}: {e}"}
        self.last_backup = {**res, "at": time.time()}
        kind = "backup.made" if res["ok"] else "backup.failed"

        def fn(conn):
            now = time.time()
            insert_event(conn, now, kind, src="argus", dst="backup", data=res)
            if not res["ok"]:
                add_message(conn, now, "ntfy", ntfy_message("Argus backup failed", str(res.get("problem"))[:300],
                                                            priority="high", tags=["warning"]),
                            dedupe_key=f"backup-failed:{time.strftime('%Y%m%d', time.localtime(now))}")

        await self.store.write(fn)
        if not res["ok"]:
            self.outbox.poke()
        elif self.cfg.backup.copy_to:
            await self.jobs.enqueue("backup", "copy", {"file": res["file"], "to": self.cfg.backup.copy_to,
                                                       "keep": self.cfg.backup.keep},
                                    needs=["desktop"], priority=20, dedupe_key=f"backup:{res['file']}",
                                    source="argus")
        return res

    async def notify_from_job(self, job_id: str, worker: str, key: str, message: dict) -> bool:
        """ctx.notify(): queue an ntfy message for a job the worker holds. The key makes a retried step's
        message a no-op. Returns True if it was queued now."""

        def fn(conn) -> bool:
            now = time.time()
            job = self.jobs._get(conn, job_id)
            self.jobs._check_lease(job, worker, now)
            oid = add_message(conn, now, "ntfy", message, dedupe_key=f"notify:{key}", job_id=job_id)
            if oid is not None:
                insert_event(conn, now, "notify.queued", job_id=job_id, src=job.plugin, dst="argus",
                             data={"title": message.get("title")})
            return oid is not None

        queued = await self.store.write(fn)
        if queued:
            self.outbox.poke()
        return queued

    async def register_plugins(self) -> None:
        from .registry import _upsert_component

        def fn(conn) -> None:
            now = time.time()
            for pid, p in self.plugin_host.plugins.items():
                node = p.manifest.helios.node
                _upsert_component(conn, now, pid, "plugin", node.label or p.manifest.name, node.group,
                                  {"version": p.manifest.version, "live": p.live, "kind": p.manifest.kind})

        await self.store.write(fn)

    def path_rules(self) -> dict[str, list[str]]:
        """Folders any plugin may touch at all (argus.yaml paths), on top of each manifest's own list."""
        return {"allowed": [str(p) for p in self.cfg.paths.allowed],
                "blocked": [str(p) for p in self.cfg.paths.blocked]}

    async def approval_tick(self) -> None:
        done = await self.approvals.tick()
        if done["reminded"] or done["expired"]:
            self.outbox.poke()

    async def prune_events(self) -> int:
        """Delete events past the retention period, a chunk per watchdog tick, until caught up; then rest
        for an hour."""
        now = time.time()
        if now < self._next_prune:
            return 0
        cutoff = now - self.cfg.events.retention_days * 86400
        n = await self.store.write(lambda conn: prune_events(conn, cutoff))
        self.pruned_events += n
        if n == 0:
            self._next_prune = now + 3600
        else:
            log.info("old events pruned", extra={"count": n})
        return n

    async def stop(self) -> None:
        await self.watchdog.stop()
        await asyncio.to_thread(self.marker.stop)  # a clean stop: no "Argus is back" message next time
        await self.phone.stop()
        await self.relay.stop()
        await self.outbox.stop()
        await self.hub.stop()
        self.store.close()
        log.info("argus stopped")

    def health(self) -> dict:
        db = self.store.health()
        ok = db["ok"] and self.watchdog.alive and self.hub.alive and self.outbox.alive and self.relay.alive
        ok = ok and self.phone.alive
        return {
            "status": "ok" if ok else "degraded",
            "version": self.version,
            "instance": self.cfg.instance.name,
            "host": self.cfg.instance.host,
            "uptime_seconds": round(time.time() - self.started_at, 1) if self.started_at else 0,
            "database": db,
            "events": self.hub.stats(),
            "outbox": self.outbox.health(),
            "replies": self.relay.health(),
            "phone": self.phone.health(),
            "watchdog": {
                "alive": self.watchdog.alive,
                "runs": self.watchdog.runs,
                "last_error": self.watchdog.last_error,
            },
        }
