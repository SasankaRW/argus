"""Watchdog: a small background task that repairs things nobody else will.

Today it requeues jobs whose worker stopped heartbeating. Approval expiry joins it in C8.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from .store import JobStore

log = logging.getLogger("argus.watchdog")


class Watchdog:
    def __init__(self, jobs: JobStore, interval: float = 5.0):
        self.jobs = jobs
        self.interval = interval
        self._task: asyncio.Task | None = None
        self.runs = 0
        self.last_error: str | None = None

    async def tick(self) -> list[str]:
        moved = await self.jobs.expire_leases()
        if moved:
            log.warning("leases expired", extra={"jobs": moved})
        self.runs += 1
        return moved

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as e:  # keep going; the next tick may succeed
                self.last_error = str(e)
                log.exception("watchdog tick failed")
            await asyncio.sleep(self.interval)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="argus-watchdog")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    @property
    def alive(self) -> bool:
        return self._task is not None and not self._task.done()
