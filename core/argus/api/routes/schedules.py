"""Schedules, file triggers and signed webhooks."""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Request

from ...cron import next_run
from ..schemas import FileTrigger, ScheduleEdit
from ._deps import Deps


def build_router(deps: Deps) -> APIRouter:
    argus = deps.argus
    router = APIRouter()
    guarded = deps.guarded

    @router.get("/schedules", dependencies=guarded)
    async def schedules() -> list[dict]:
        return await argus.scheduler.list()

    @router.patch("/schedules/{sid}", dependencies=guarded)
    async def schedule_edit(sid: str, body: ScheduleEdit) -> dict:
        """Pause or resume one of your schedules (the ones from argus.yaml and plugins follow their files)."""
        def fn(c):
            r = c.execute("SELECT owner, cron FROM schedules WHERE id = ?", (sid,)).fetchone()
            if r is None or r["owner"] != "you":
                return False
            nxt = next_run(r["cron"], time.time()) if body.enabled else None
            c.execute("UPDATE schedules SET enabled = ?, next_run_at = COALESCE(?, next_run_at), updated_at = ?"
                      " WHERE id = ?", (int(body.enabled), nxt, time.time(), sid))
            return True
        if not await argus.store.write(fn):
            raise HTTPException(status_code=404, detail=f"no schedule of yours called {sid}")
        return {"ok": True}

    @router.delete("/schedules/{sid}", dependencies=guarded)
    async def schedule_delete(sid: str) -> dict:
        n = await argus.store.write(lambda c: c.execute("DELETE FROM schedules WHERE id = ? AND owner = 'you'",
                                                        (sid,)).rowcount)
        if not n:
            raise HTTPException(status_code=404, detail=f"no schedule of yours called {sid}")
        return {"ok": True}

    @router.post("/schedules/{sid}/run", dependencies=guarded)
    async def schedule_run(sid: str) -> dict:
        job_id = await argus.scheduler.run_now(sid)
        if job_id is None:
            raise HTTPException(status_code=404, detail=f"no schedule {sid}")
        return {"job_id": job_id}

    @router.post("/triggers/file", dependencies=guarded)
    async def file_trigger(body: FileTrigger) -> dict:
        await argus.registry.touch_worker(body.worker)
        return await argus.triggers.file(body.worker, body.trigger, body.path, body.sha256, body.size)

    @router.post("/hooks/{name}")
    async def webhook(name: str, request: Request) -> dict:
        body = await request.body()
        return await argus.triggers.webhook(name, request.headers, body)

    return router
