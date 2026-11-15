"""Controls: stop, start, restart and pause plugins and workers, and everything at once (docs/controls.md).

All behind the token. The core has no route here: its id gets 403 on every one of them.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ...controls import ControlError
from ..schemas import ControlBody
from ._deps import Deps


def build_router(deps: Deps) -> APIRouter:
    argus = deps.argus
    router = APIRouter()
    guarded = deps.guarded
    controls = argus.controls

    async def run(call, *args) -> dict:
        try:
            return await call(*args)
        except ControlError as e:
            raise HTTPException(status_code=e.status, detail=e.detail) from None

    @router.post("/plugins/{pid}/stop", dependencies=guarded)
    async def plugin_stop(pid: str, body: ControlBody | None = None) -> dict:
        """Pause a plugin: its queued jobs wait, nothing new is queued for it (409 for a job made by the API), the job
        it runs now finishes. Lasts until Start, also across an Argus restart."""
        return await run(controls.stop_plugin, pid, body.reason if body else None)

    @router.post("/plugins/{pid}/start", dependencies=guarded)
    async def plugin_start(pid: str) -> dict:
        return await run(controls.start_plugin, pid)

    @router.post("/plugins/{pid}/restart", dependencies=guarded)
    async def plugin_restart(pid: str) -> dict:
        """Start, requeue the plugin's dead or expired leases, and make workers load its code again."""
        return await run(controls.restart_plugin, pid)

    @router.post("/workers/{worker_id}/pause", dependencies=guarded)
    async def worker_pause(worker_id: str, body: ControlBody | None = None) -> dict:
        """The worker's claims return nothing (it keeps heartbeating and finishes the job it has)."""
        return await run(controls.pause_worker, worker_id, body.reason if body else None)

    @router.post("/workers/{worker_id}/resume", dependencies=guarded)
    async def worker_resume(worker_id: str) -> dict:
        return await run(controls.resume_worker, worker_id)

    @router.post("/workers/{worker_id}/restart", dependencies=guarded)
    async def worker_restart(worker_id: str) -> dict:
        """The worker finishes its job, then exits with code 75 and its supervisor or systemd starts it again."""
        return await run(controls.restart_worker, worker_id)

    @router.get("/control/state", dependencies=guarded)
    async def control_state() -> dict:
        return await controls.state()

    @router.post("/control/pause-all", dependencies=guarded)
    async def pause_all() -> dict:
        """Pause every plugin and worker (not the core, the API, Helios, the backups or approvals)."""
        return await run(controls.pause_all)

    @router.post("/control/resume-all", dependencies=guarded)
    async def resume_all() -> dict:
        """Undo pause-all: only what it paused; what you paused yourself stays paused."""
        return await run(controls.resume_all)

    return router
