"""The inbox, approvals and their phone page."""

from __future__ import annotations

import json
import time

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import HTMLResponse

from ...approvals import ApprovalNotFound, BadToken
from ...ari import ari as ari_mod
from ...outbox import PRIORITIES, phone_message
from .. import approval_page
from ..schemas import AskApproval, Decide, Notify
from ._deps import Deps


def build_router(deps: Deps) -> APIRouter:
    argus = deps.argus
    router = APIRouter()
    guarded = deps.guarded
    token_ok = deps.token_ok

    @router.get("/inbox", dependencies=guarded)
    async def inbox() -> dict:
        """Approvals, lessons the nightly review proposes, and Ari's open questions, newest first."""
        approvals = await argus.approvals.list("pending", limit=100)

        def fn(c):
            lessons = [dict(r) for r in c.execute(
                "SELECT l.id, l.playbook, l.text, l.evals, l.created_at, p.plugin, p.name FROM lessons l"
                " LEFT JOIN playbooks p ON p.key = l.playbook WHERE l.state = 'proposed' ORDER BY l.created_at DESC")]
            return lessons, ari_mod.open_questions(c, time.time() - 7 * 86400)

        lessons, questions = await argus.store.read(fn)
        items = [{"kind": "approval", "id": a["id"], "title": a["title"], "plugin": a.get("plugin"),
                  "type": a.get("type"), "at": a["created_at"]} for a in approvals]
        items += [{"kind": "lesson", "id": x["id"], "title": f"A lesson for {x.get('name') or x['playbook']}",
                   "text": x["text"],
                   "plugin": x.get("plugin"), "evals": json.loads(x["evals"]) if x.get("evals") else None,
                   "at": x["created_at"]} for x in lessons]
        items += [{"kind": "question", "id": q["conv"], "title": q["text"] or "Ari asks", "chat": q["title"],
                   "plugin": "ari", "at": q["at"]} for q in questions]
        items.sort(key=lambda x: -x["at"])
        return {"items": items, "counts": {k: sum(1 for i in items if i["kind"] == k)
                                           for k in ("approval", "lesson", "question")}}


    @router.post("/jobs/{job_id}/approvals", dependencies=guarded)
    async def ask_approval(job_id: str, body: AskApproval) -> dict:
        a, created = await argus.approvals.request(
            job_id, body.worker, body.key, body.type, body.title, fields=body.fields, items=body.items,
            summary=body.summary, link=body.link, step=body.step, image=body.image)
        if created:
            argus.outbox.poke()
        return {**a, "created": created}

    @router.post("/jobs/{job_id}/notify", dependencies=guarded)
    async def notify(job_id: str, body: Notify) -> dict:
        if body.priority not in PRIORITIES:
            raise HTTPException(status_code=422, detail=f"priority must be one of {', '.join(PRIORITIES)}")
        msg = phone_message(body.title, body.text, priority=body.priority, tags=body.tags, click=body.link)
        return {"queued": await argus.notify_from_job(job_id, body.worker, body.key, msg)}

    @router.get("/approvals", dependencies=guarded)
    async def list_approvals(state: str | None = None, job: str | None = None, limit: int = 100) -> list[dict]:
        return await argus.approvals.list(state, job, min(max(limit, 1), 500))

    @router.get("/approvals/{approval_id}", dependencies=guarded)
    async def get_approval(approval_id: str) -> dict:
        return await argus.approvals.get(approval_id)

    @router.post("/approvals/{approval_id}/decide")
    async def decide(approval_id: str, request: Request, t: str | None = None, answer: str | None = None,
                     authorization: str | None = Header(default=None)) -> dict:
        """Helios and apps send the Argus token; the phone buttons send ?t= (signed, one-time) instead."""
        raw = await request.body()
        try:
            body = Decide.model_validate_json(raw) if raw.strip() else Decide()
        except ValueError as e:
            raise HTTPException(status_code=422, detail=f"bad body: {e}") from None
        ans = body.answer or answer
        if ans not in ("approve", "reject"):
            raise HTTPException(status_code=422, detail="answer must be approve or reject")
        if t is None and not token_ok(authorization):
            raise HTTPException(status_code=401, detail="missing or wrong token")
        by = body.by or ("phone" if t is not None else "helios")
        a = await argus.approvals.decide(approval_id, ans, fields=body.fields, by=by, token=t)
        return {"id": a["id"], "state": a["state"], "answer": a["answer"]}

    @router.get("/a/{approval_id}", response_class=HTMLResponse, include_in_schema=False)
    async def approval_page_view(approval_id: str, t: str = "") -> HTMLResponse:
        try:
            a = await argus.approvals.verify_link(approval_id, t)
        except (ApprovalNotFound, BadToken):
            return HTMLResponse(approval_page.invalid(), status_code=404)
        return HTMLResponse(approval_page.render(a, t), headers={
            "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            # only this page's own inline style and script, talking to this Argus
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline';"
                                       " connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none';"
                                       " frame-ancestors 'none'"})

    return router
