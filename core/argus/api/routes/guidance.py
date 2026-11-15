"""The guidance loop: samples, playbooks, evals, verdicts and lessons."""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Query

from ... import guidance
from ...tools import Tool
from ..schemas import LessonDecide, Replay, SampleIn, Verdict
from ._deps import Deps


def build_router(deps: Deps) -> APIRouter:
    argus = deps.argus
    router = APIRouter()
    guarded = deps.guarded
    tools = deps.tools

    @router.post("/jobs/{job_id}/samples", dependencies=guarded)
    async def add_sample(job_id: str, body: SampleIn) -> dict:
        """A model answer from a plugin's job (ctx.llm), kept for your verdict and the nightly review."""
        job = await argus.jobs.get(job_id)
        sid = await argus.store.write(lambda c: guidance.add_sample(
            c, time.time(), job_id=job_id, plugin=job.plugin, playbook=body.playbook, schema=body.schema_,
            input=body.input, output=body.output, tier=body.tier, escalated=body.escalated,
            keep=argus.cfg.guidance.keep_samples, subject=body.subject, used=body.used))
        return {"id": sid}

    @router.get("/jobs/{job_id}/samples", dependencies=guarded)
    async def job_samples(job_id: str) -> list[dict]:
        """A job's model answers, each with the lessons that were in force ("lessons used")."""
        return await argus.store.read(lambda c: [guidance.sample_json(r, c) for r in c.execute(
            "SELECT * FROM samples WHERE job_id = ? ORDER BY id", (job_id,))])

    @router.get("/guidance/{key}/trend", dependencies=guarded)
    async def playbook_trend(key: str, weeks: int = Query(8, ge=1, le=52)) -> dict:
        """Is it learning? Per week how often the first model was right and how often it escalated, and when lessons
        were approved."""
        return await argus.store.read(lambda c: guidance.trend(c, key, time.time(), weeks))

    @router.get("/guidance", dependencies=guarded)
    async def guidance_overview(plugin: str | None = None) -> list[dict]:
        """Per playbook: answers kept, escalations, your verdicts, lessons in force and waiting."""
        return await argus.store.read(lambda c: guidance.overview(c, plugin))

    @router.get("/guidance/{key}/samples", dependencies=guarded)
    async def playbook_samples(key: str, verdict: str | None = None, limit: int = Query(50, ge=1, le=500)
                               ) -> list[dict]:
        where, args = "playbook = ?", [key]
        if verdict in ("correct", "wrong"):
            where, args = where + " AND verdict = ?", args + [verdict]
        elif verdict == "none":
            where += " AND verdict IS NULL"
        return await argus.store.read(lambda c: [guidance.sample_json(r) for r in c.execute(
            f"SELECT * FROM samples WHERE {where} ORDER BY id DESC LIMIT ?", (*args, limit))])

    @router.post("/samples/{sample_id}/verdict", dependencies=guarded)
    async def sample_verdict(sample_id: int, body: Verdict) -> dict:
        """Correct (joins the eval set) or wrong (with what it should have been; the review learns from it)."""
        if not await argus.store.write(lambda c: guidance.set_verdict(c, sample_id, body.verdict, body.correction)):
            raise HTTPException(status_code=404, detail="no such answer")
        return {"ok": True}

    @router.get("/guidance/{key}/evals", dependencies=guarded)
    async def playbook_evals(key: str) -> dict:
        """A playbook's tests (the eval set) and the history of test runs (pass rate)."""
        def fn(c):
            return {"tests": [guidance.sample_json(r) for r in guidance.eval_samples(c, key)],
                    "runs": guidance.runs(c, key)}

        return await argus.store.read(fn)

    @router.post("/guidance/{key}/evals", dependencies=guarded)
    async def playbook_evals_run(key: str, compare: bool = False) -> dict:
        """Run the tests now (the first local tier, with the lessons in force). `compare`: also without the lessons,
        so you see what they change."""
        r = await argus.queue_evals(key, compare=compare)
        if r is None:
            raise HTTPException(status_code=404, detail="no such playbook")
        return r

    @router.post("/samples/{sample_id}/replay", dependencies=guarded)
    async def sample_replay(sample_id: int, body: Replay) -> dict:
        """Ask another tier the same question; the job's result has both answers."""
        if body.tier not in argus.cfg.models.tiers:
            have = ", ".join(argus.cfg.models.tiers)
            raise HTTPException(status_code=422, detail=f"no tier {body.tier} (have {have})")
        r = await argus.queue_replay(sample_id, body.tier)
        if r is None:
            raise HTTPException(status_code=404, detail="no such answer")
        return r

    @router.post("/guidance/review", dependencies=guarded)
    async def guidance_review() -> dict:
        """Review now: Claude proposes lessons for playbooks with new mistakes; you approve them."""
        return await argus.guidance_review()

    @router.get("/guidance/ari-cases", dependencies=guarded)
    async def ari_cases() -> list[dict]:
        """Ari's picks you marked Correct, as cases for `python -m argus.ari.ari_eval` (it adds them to its own)."""
        return await argus.store.read(guidance.ari_cases)

    @router.post("/guidance/followups", dependencies=guarded)
    async def guidance_followups() -> dict:
        """Look now whether you moved back, renamed again or moved elsewhere what jobs did (runs every
        `guidance.followup_hours` anyway)."""
        return {"jobs": await argus.guidance_followups()}

    @router.post("/lessons/{lesson_id}/decide", dependencies=guarded)
    async def lesson_decide(lesson_id: int, body: LessonDecide) -> dict:
        r = await argus.store.write(lambda c: guidance.decide(c, lesson_id, body.approve, time.time()))
        if r is None:
            raise HTTPException(status_code=409, detail="that lesson isn't waiting for a decision")
        return r

    @router.delete("/lessons/{lesson_id}", dependencies=guarded)
    async def lesson_drop(lesson_id: int) -> dict:
        """Stop using lessons that are in force (the playbook is used as written again)."""
        if not await argus.store.write(lambda c: guidance.drop_active(c, lesson_id)):
            raise HTTPException(status_code=404, detail="no such lessons in force")
        return {"ok": True}

    async def _propose(x: dict) -> dict:
        lid = await argus.store.write(lambda c: guidance.propose(c, time.time(), str(x["key"]), str(x["text"]),
                                                                 x.get("evals") or {}, x.get("job_id")))
        return {"lesson": lid}

    async def _decide(x: dict) -> dict:
        r = await argus.store.write(lambda c: guidance.decide(c, int(x["lesson"]), bool(x["approve"]), time.time()))
        return r or {"state": "already decided"}

    tools.builtin["propose_lessons"] = Tool(
        "propose_lessons", "(guidance loop) keep proposed lessons for a playbook",
        {"key": {"type": "string"}, "text": {"type": "string"}, "evals": {"type": "object"},
         "job_id": {"type": "string"}}, ["key", "text"], fn=_propose, for_ari=False, for_mcp=False)
    async def _record(x: dict) -> dict:
        rid = await argus.store.write(lambda c: guidance.record_run(c, time.time(), str(x["key"]), x.get("run") or {},
                                                                    str(x.get("why") or "manual"), x.get("job_id")))
        return {"run": rid}

    tools.builtin["record_evals"] = Tool(
        "record_evals", "(guidance loop) keep the result of a test run",
        {"key": {"type": "string"}, "run": {"type": "object"}, "why": {"type": "string"}, "job_id": {"type": "string"}},
        ["key", "run"], fn=_record, for_ari=False, for_mcp=False)
    async def _followed(x: dict) -> dict:
        return await argus.store.write(lambda c: guidance.record_followups(c, time.time(), list(x.get("results")
                                                                                               or [])))

    async def _drop_pictures(x: dict) -> dict:
        from ... import approvals as approvals_mod

        jid = str(x.get("job") or "")
        return {"dropped": await argus.store.write(lambda c: approvals_mod.drop_pictures(c, jid)) if jid else 0}

    tools.builtin["forget_task_pictures"] = Tool(
        "forget_task_pictures", "(Ari's Workstation) the task's screenshots are deleted when it ends",
        {"job": {"type": "string"}}, ["job"], fn=_drop_pictures, for_ari=False, for_mcp=False)
    tools.builtin["record_followups"] = Tool(
        "record_followups", "(guidance loop) what you did with files a job moved or renamed",
        {"results": {"type": "array"}}, ["results"], fn=_followed, for_ari=False, for_mcp=False)
    tools.builtin["decide_lessons"] = Tool(
        "decide_lessons", "(guidance loop) your answer to proposed lessons",
        {"lesson": {"type": "integer"}, "approve": {"type": "boolean"}}, ["lesson", "approve"], fn=_decide,
        for_ari=False, for_mcp=False)

    return router
