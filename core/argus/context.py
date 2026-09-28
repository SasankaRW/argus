"""The running Argus instance: config, store, jobs and background tasks, started and stopped together."""

from __future__ import annotations

import logging
import time

from . import __version__
from .config import Config
from .db import Store
from .events import EventHub, prune_events
from .jobs import JobStore, Watchdog
from .registry import Registry

log = logging.getLogger("argus")


class Argus:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.version = __version__
        self.store = Store(cfg.db_path)
        self.jobs = JobStore(self.store, cfg.jobs)
        self.registry = Registry(self.store)
        self.hub = EventHub(self.store, queue_size=cfg.events.stream_queue)
        stale_after = cfg.jobs.heartbeat_seconds * 4
        self.watchdog = Watchdog(
            self.jobs,
            cfg.jobs.watchdog_interval_seconds,
            extra=[lambda: self.registry.mark_stale_workers(stale_after), self.prune_events],
        )
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
        await self.hub.start()
        self.watchdog.start()
        self.started_at = time.time()
        log.info("argus started", extra={"version": self.version, "instance": self.cfg.instance.name})

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
        await self.hub.stop()
        self.store.close()
        log.info("argus stopped")

    def health(self) -> dict:
        db = self.store.health()
        ok = db["ok"] and self.watchdog.alive and self.hub.alive
        return {
            "status": "ok" if ok else "degraded",
            "version": self.version,
            "instance": self.cfg.instance.name,
            "host": self.cfg.instance.host,
            "uptime_seconds": round(time.time() - self.started_at, 1) if self.started_at else 0,
            "database": db,
            "events": self.hub.stats(),
            "watchdog": {
                "alive": self.watchdog.alive,
                "runs": self.watchdog.runs,
                "last_error": self.watchdog.last_error,
            },
        }
