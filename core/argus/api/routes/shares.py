"""Share (the phone's share menu): targets, uploads and sending."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse

from ...config import PRIORITY_INTERACTIVE
from ...shares import ShareError, ShareStore, kinds_of
from ..schemas import NewShare, SendShare
from ._deps import Deps


def build_router(deps: Deps) -> APIRouter:
    argus = deps.argus
    router = APIRouter()
    guarded = deps.guarded

    shares = ShareStore(argus.cfg.db_path.parent / "shares", argus.cfg.share.max_mb, argus.cfg.share.keep_days)

    def share_targets() -> list[dict]:
        return [{"plugin": pid, "name": p.manifest.name, **t.model_dump()}
                for pid, p in sorted(argus.plugin_host.plugins.items()) for t in p.manifest.share]

    def share_or_404(fn, *a):
        try:
            return fn(*a)
        except ShareError as e:
            raise HTTPException(status_code=404 if "no such" in str(e) or "bad" in str(e) else e.status,
                                detail=str(e)) from None

    @router.get("/share/targets", dependencies=guarded)
    async def get_share_targets() -> list[dict]:
        return share_targets()

    @router.post("/shares", dependencies=guarded)
    async def new_share(body: NewShare) -> dict:
        return await asyncio.to_thread(shares.create, body.title, body.text, body.url, body.note)

    @router.put("/shares/{sid}/files", dependencies=guarded)
    async def add_share_file(sid: str, request: Request, name: str = Query(..., max_length=200),
                             type: str = Query("application/octet-stream", max_length=100)) -> dict:
        meta = share_or_404(shares.meta, sid)  # before reading the body
        if meta["job_id"]:
            raise HTTPException(status_code=409, detail="already sent")
        data = bytearray()
        async for chunk in request.stream():
            data += chunk
            if len(data) > shares.max_bytes:
                raise HTTPException(status_code=413, detail=f"too big: shares are limited to "
                                                            f"{shares.max_bytes // (1024 * 1024)} MB")
        return await asyncio.to_thread(share_or_404, shares.add_file, sid, name, type, bytes(data))

    @router.get("/shares/{sid}", dependencies=guarded)
    async def get_share(sid: str) -> dict:
        return share_or_404(shares.meta, sid)

    @router.get("/shares/{sid}/files/{name}", dependencies=guarded)
    async def get_share_file(sid: str, name: str):
        path, mime = share_or_404(shares.file_path, sid, name)
        # only pictures and PDFs open in the browser; anything else (html, svg, ...) downloads, never runs here
        inline = (mime.startswith("image/") and mime != "image/svg+xml") or mime == "application/pdf"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="no such file in this share")
        return FileResponse(path, media_type=mime if inline else "application/octet-stream", filename=name,
                            content_disposition_type="inline" if inline else "attachment",
                            headers={"X-Content-Type-Options": "nosniff",
                                     "Content-Security-Policy": "sandbox; default-src 'none'"})

    @router.post("/shares/{sid}/send", dependencies=guarded)
    async def send_share(sid: str, body: SendShare) -> dict:
        """Queue the share for the plugin you picked (interactive priority)."""
        meta = share_or_404(shares.meta, sid)
        if meta["job_id"]:
            return {"id": meta["job_id"], "created": False}
        t = next((x for x in share_targets() if x["plugin"] == body.plugin and x["workflow"] == body.workflow), None)
        if t is None:
            raise HTTPException(status_code=422, detail=f"{body.plugin}.{body.workflow} does not take shares")
        have = kinds_of(meta)
        if not have:
            raise HTTPException(status_code=422, detail="nothing to send: add a file, a link or some text")
        if not have & set(t["accepts"]):
            raise HTTPException(status_code=422, detail=f"{t['label']} takes {', '.join(t['accepts'])}")
        p = argus.plugin_host.plugins[body.plugin]
        job_id, created = await argus.jobs.enqueue(
            body.plugin, body.workflow,
            {"share": sid, "title": meta["title"], "text": meta["text"], "url": meta["url"], "note": meta["note"],
             "files": meta["files"]},
            needs=p.manifest.job_needs(), priority=PRIORITY_INTERACTIVE, dedupe_key=f"share:{sid}", source="helios")
        await asyncio.to_thread(shares.mark_sent, sid, job_id)
        return {"id": job_id, "created": created}

    return router
