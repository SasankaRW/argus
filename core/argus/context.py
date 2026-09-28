"""The running Argus instance: config, store, jobs and background tasks, started and stopped together."""

from __future__ import annotations

import logging
import time

from . import __version__
from .config import Config
from .db import Store
from .jobs import JobStore, Watchdog

log = logging.getLogger("argus")


class Argus:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.version = __version__
        self.store = Store(cfg.db_path)
        self.jobs = JobStore(self.store, cfg.jobs)
        self.watchdog = Watchdog(self.jobs, cfg.jobs.watchdog_interval_seconds)
        self.started_at: float | None = None

    def open(self) -> Argus:
        """Synchronous part of startup: open the database and run migrations."""
        self.store.open()
        return self

    async def start(self) -> None:
        """Async part of startup: background tasks."""
        self.watchdog.start()
        self.started_at = time.time()
        log.info("argus started", extra={"version": self.version, "instance": self.cfg.instance.name})

    async def stop(self) -> None:
        await self.watchdog.stop()
        self.store.close()
        log.info("argus stopped")

    def health(self) -> dict:
        db = self.store.health()
        ok = db["ok"] and self.watchdog.alive
        return {
            "status": "ok" if ok else "degraded",
            "version": self.version,
            "instance": self.cfg.instance.name,
            "host": self.cfg.instance.host,
            "uptime_seconds": round(time.time() - self.started_at, 1) if self.started_at else 0,
            "database": db,
            "watchdog": {
                "alive": self.watchdog.alive,
                "runs": self.watchdog.runs,
                "last_error": self.watchdog.last_error,
            },
        }
