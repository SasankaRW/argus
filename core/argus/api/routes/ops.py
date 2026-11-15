"""The morning brief, time saved, backups and logs."""

from __future__ import annotations

import asyncio
import re
import time

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from ... import complogs, daily, logview
from ._deps import Deps


def build_router(deps: Deps) -> APIRouter:
    argus = deps.argus
    router = APIRouter()
    guarded = deps.guarded

    @router.post("/summary", dependencies=guarded)
    async def summary_now() -> dict:
        """Send the evening summary now (to try it)."""
        return await argus.send_summary()

    @router.get("/time-saved", dependencies=guarded)
    async def get_time_saved(days: int = Query(7, ge=1, le=366)) -> dict:
        """What the plugins saved you: total, per plugin, per day."""
        return await argus.store.read(lambda c: daily.time_saved(c, time.time(), days))

    @router.post("/brief", dependencies=guarded)
    async def brief_now() -> dict:
        """Send the morning brief now (to try it)."""
        return await argus.send_brief()


    @router.get("/backups", dependencies=guarded)
    async def backups() -> dict:
        return {"files": argus.backups.list(), "last": argus.last_backup, "at": argus.cfg.backup.at,
                "keep": argus.cfg.backup.keep, "copy_to": argus.cfg.backup.copy_to,
                "enabled": argus.cfg.backup.enabled}

    @router.post("/backups", dependencies=guarded)
    async def backup_now() -> dict:
        return await argus.backup_now()

    @router.get("/backups/files/{name}", dependencies=guarded)
    async def backup_file(name: str):
        if not re.fullmatch(r"argus-\d{8}-\d{6}\.db", name) or not (argus.backups.dir / name).is_file():
            raise HTTPException(status_code=404, detail="no such backup")
        return FileResponse(argus.backups.dir / name, media_type="application/octet-stream", filename=name)

    @router.get("/logs", dependencies=guarded)
    async def log_sources() -> list[dict]:
        """The log files Helios can show: argusd, the worker(s) on this machine, Ollama."""
        out = []
        for name, path in logview.sources(argus.cfg.log_dir).items():
            try:
                st = path.stat()
            except OSError:
                continue
            out.append({"name": name, "size": st.st_size, "modified": st.st_mtime})
        return out

    @router.get("/logs/{name}", dependencies=guarded, response_model=None)
    async def log_read(name: str, after: int | None = Query(None, ge=0), lines: int | None = Query(None, ge=1),
                       level: str | None = Query(None, pattern="^(all|error)$"),
                       since: float | None = Query(None, ge=0)):
        """Two answers. With `level` or `since` (or for a name that is no log file, e.g. a plugin or worker id) the
        newest-last JSON lines [{ts, level, source, msg}] of one component: a plugin, a worker or "argus" (log files
        plus its recent events; lines max 500). Otherwise a log file as before ({entries, offset}: Helios's Logs
        page)."""
        path = logview.sources(argus.cfg.log_dir).get(name)
        if after is not None or (path is not None and level is None and since is None):
            if path is None:
                raise HTTPException(status_code=404, detail=f"no log {name!r}")
            return await asyncio.to_thread(logview.read, path, name, after=after, lines=min(lines or 300, 2000))
        plugins = set(argus.plugin_host.plugins)

        def collect(conn):
            return complogs.collect(conn, argus.cfg, plugins, name, lines or 200, level or "all", since)

        try:
            return await argus.store.read(collect)
        except complogs.UnknownComponent:
            raise HTTPException(status_code=404, detail=f"no component or log {name!r}") from None

    return router
