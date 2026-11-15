"""Jobs: submit, list, read, undo and Wrong on a job's file changes, events, cancel, rerun and resume."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, HTTPException

from ... import guidance
from ...config import PRIORITY_INTERACTIVE
from ...jobs import JobState
from ..schemas import Fix, SubmitJob
from ._deps import Deps, job_json


def build_router(deps: Deps) -> APIRouter:
    argus = deps.argus
    router = APIRouter()
    guarded = deps.guarded
    tools = deps.tools

    @router.post("/jobs", status_code=201, dependencies=guarded)
    async def submit(body: SubmitJob) -> dict:
        job_id, created = await argus.jobs.enqueue(
            body.plugin, body.workflow, body.input,
            needs=await tools.needs_now([*body.needs, *argus.plugin_host.needs_for(body.plugin)]),
            priority=body.priority,
            dedupe_key=body.dedupe_key, max_attempts=body.max_attempts, delay=body.delay,
            model_group=body.model, window=body.window,
        )
        return {"id": job_id, "created": created}

    @router.get("/jobs", dependencies=guarded)
    async def list_jobs(state: JobState | None = None, limit: int = 100, plugin: str | None = None,
                        before: float | None = None) -> list[dict]:
        return [job_json(j) for j in await argus.jobs.list_jobs(state, min(max(limit, 1), 500), plugin, before)]

    @router.get("/queue", dependencies=guarded)
    async def queue() -> dict:
        """Running, waiting for you, and queued in the order they will run, each with why it waits."""
        return await argus.jobs.queue(await argus.registry.workers())

    @router.get("/jobs/counts", dependencies=guarded)
    async def job_counts() -> dict:
        return await argus.jobs.counts()

    @router.get("/jobs/{job_id}", dependencies=guarded)
    async def get_job(job_id: str) -> dict:
        return job_json(await argus.jobs.get(job_id), await argus.jobs.steps(job_id))

    async def _changes(job_id: str) -> tuple[Any, list[dict]]:
        """A job's file changes (the undo log), each with the undo job made for it, if any."""
        job = await argus.jobs.get(job_id)
        evs = [e for e in await argus.jobs.events(job_id) if e["kind"].startswith("file.")]

        def undos(conn):
            keys = [f"undo:{e['id']}" for e in evs]
            if not keys:
                return {}
            keys += [f"fix:{e['id']}" for e in evs]
            rows = conn.execute("SELECT id, state, dedupe_key, input FROM jobs WHERE dedupe_key IN ({})"
                                " ORDER BY created_at".format(",".join("?" * len(keys))), keys).fetchall()
            out: dict[str, dict] = {}
            for r in rows:  # latest wins
                what, eid = r["dedupe_key"].split(":", 1)
                out[f"{what}:{eid}"] = {"job_id": r["id"], "state": r["state"],
                                        "value": json.loads(r["input"] or "{}").get("value")}
            return out

        done = await argus.store.read(undos)
        p = argus.plugin_host.plugins.get(job.plugin)
        can = p is not None and "undo" in p.manifest.all_workflows()
        wrong = p is not None and p.manifest.helios.wrong is not None
        out = []
        for e in evs:
            d = e["data"] or {}
            u, f = done.get(f"undo:{e['id']}"), done.get(f"fix:{e['id']}")
            real = e["kind"] == "file.moved" and not d.get("dry_run")
            free = all(x is None or x["state"] in ("dead", "cancelled") for x in (u, f))
            out.append({"event_id": e["id"], "kind": e["kind"], "at": e["at"], "from": d.get("from"),
                        "to": d.get("to"), "path": d.get("path"), "dry_run": bool(d.get("dry_run")), "undo": u,
                        "fix": f, "can_undo": can and real and free, "can_fix": wrong and real and free})
        return job, out

    change_lock = asyncio.Lock()

    @router.post("/jobs/{job_id}/changes/{event_id}/wrong", dependencies=guarded)
    async def wrong_change(job_id: str, event_id: str, body: Fix) -> dict:
        """The "Wrong" button: the plugin's correction workflow puts the file where it belongs and remembers it."""
        async with change_lock:  # Undo and Wrong on the same change: one at a time
            job, changes = await _changes(job_id)
            c = next((x for x in changes if x["event_id"] == event_id), None)
            if c is None:
                raise HTTPException(status_code=404, detail="no such change in this job")
            if not c["can_fix"]:
                if c["fix"]:
                    return {"id": c["fix"]["job_id"], "created": False}
                raise HTTPException(status_code=409, detail="this change can't be corrected (dry-run, already undone, "
                                                            "or the plugin has no Wrong button)")
            p = argus.plugin_host.plugins[job.plugin]
            c_from = str(c["from"] or "")
            wf = p.manifest.helios.wrong.workflow  # type: ignore[union-attr]
            job_id2, created = await argus.jobs.enqueue(
                job.plugin, wf, {"from": c["to"], "orig": c["from"], "value": body.value, "fix_of": event_id,
                                 "job": job_id},
                needs=p.manifest.job_needs(), priority=PRIORITY_INTERACTIVE, dedupe_key=f"fix:{event_id}",
                source="helios")
            if created:  # the answer behind this change was wrong, and you said what's right
                await argus.store.write(lambda c: guidance.implicit(
                    c, job_id, c_from, "wrong", f"should have been: {body.value}" if body.value else None,
                    "wrong button"))
            return {"id": job_id2, "created": created}

    @router.get("/jobs/{job_id}/changes", dependencies=guarded)
    async def job_changes(job_id: str) -> list[dict]:
        return (await _changes(job_id))[1]

    @router.post("/jobs/{job_id}/changes/{event_id}/undo", dependencies=guarded)
    async def undo_change(job_id: str, event_id: str) -> dict:
        """Put one moved file back, through the plugin's own `undo` workflow (so its permissions still apply)."""
        async with change_lock:  # Undo and Wrong on the same change: one at a time
            job, changes = await _changes(job_id)
            c = next((x for x in changes if x["event_id"] == event_id), None)
            if c is None:
                raise HTTPException(status_code=404, detail="no such change in this job")
            if not c["can_undo"]:
                if c["undo"]:
                    return {"id": c["undo"]["job_id"], "created": False}
                raise HTTPException(status_code=409, detail="this change can't be undone (dry-run, not a move, "
                                                            "or the plugin has no undo workflow)")
            p = argus.plugin_host.plugins[job.plugin]
            job_id2, created = await argus.jobs.enqueue(
                job.plugin, "undo", {"from": c["to"], "to": c["from"], "undo_of": event_id, "job": job_id},
                needs=p.manifest.job_needs(), priority=PRIORITY_INTERACTIVE, dedupe_key=f"undo:{event_id}",
                source="helios")
            if created:  # you didn't want that: the answer behind it counts as wrong
                c_from = str(c["from"] or "")
                await argus.store.write(lambda conn: guidance.implicit(conn, job_id, c_from, "wrong", "you undid it",
                                                                       "undo"))
            return {"id": job_id2, "created": created}

    @router.get("/jobs/{job_id}/events", dependencies=guarded)
    async def job_events(job_id: str) -> list[dict]:
        await argus.jobs.get(job_id)
        return await argus.jobs.events(job_id)

    @router.post("/jobs/{job_id}/cancel", dependencies=guarded)
    async def cancel(job_id: str) -> dict:
        return job_json(await argus.jobs.cancel(job_id))

    @router.post("/jobs/{job_id}/rerun", dependencies=guarded)
    async def rerun(job_id: str) -> dict:
        return job_json(await argus.jobs.rerun(job_id))

    @router.post("/jobs/{job_id}/resume", dependencies=guarded)
    async def resume(job_id: str) -> dict:
        return job_json(await argus.jobs.resume(job_id))

    return router
