"""Plugins: detail, settings, rules, state and run."""

from __future__ import annotations

import asyncio
import json
import time

import yaml
from fastapi import APIRouter, HTTPException

from ... import daily, guidance
from ...config import PRIORITY_INTERACTIVE
from ...events import insert_event
from ..schemas import PluginSettings, RulesText, RunPlugin, StateValue
from ._deps import Deps, job_json


def build_router(deps: Deps) -> APIRouter:
    argus = deps.argus
    router = APIRouter()
    guarded = deps.guarded

    def rules_of(pid: str):
        p = argus.plugin_host.plugins.get(pid)
        if p is None or p.manifest.helios.rules is None:
            raise HTTPException(status_code=404, detail=f"{pid} has no editable rules")
        return p, p.path / p.manifest.helios.rules.file

    async def state_get(pid: str, key: str):
        def fn(conn):
            row = conn.execute("SELECT value FROM plugin_state WHERE plugin = ? AND key = ?", (pid, key)).fetchone()
            return json.loads(row[0]) if row and row[0] is not None else None
        return await argus.store.read(fn)

    async def state_set(pid: str, key: str, value) -> None:
        def fn(conn):
            now = time.time()
            conn.execute("INSERT INTO plugin_state (plugin, key, value, created_at, updated_at) VALUES (?,?,?,?,?)"
                         " ON CONFLICT(plugin, key) DO UPDATE SET value = excluded.value,"
                         " updated_at = excluded.updated_at", (pid, key, json.dumps(value), now, now))
        await argus.store.write(fn)

    @router.get("/plugins/{pid}/rules", dependencies=guarded)
    async def get_rules(pid: str) -> dict:
        p, f = rules_of(pid)
        default = f.read_text(encoding="utf-8") if f.exists() else ""
        mine = await state_get(pid, "rules")
        return {"label": p.manifest.helios.rules.label, "file": f.name, "text": mine or default,  # type: ignore
                "default": default, "custom": bool(mine), "learned": await state_get(pid, "learned")}

    @router.put("/plugins/{pid}/rules", dependencies=guarded)
    async def put_rules(pid: str, body: RulesText) -> dict:
        """Save your version. Checked here for YAML; the plugin checks the meaning at its next job."""
        rules_of(pid)
        try:
            parsed = await asyncio.to_thread(yaml.safe_load, body.text)
        except yaml.YAMLError as e:
            mark = getattr(e, "problem_mark", None)
            where = f" (line {mark.line + 1})" if mark else ""
            raise HTTPException(status_code=422, detail=f"not valid YAML{where}: {getattr(e, 'problem', e)}") from None
        if not isinstance(parsed, dict):
            raise HTTPException(status_code=422, detail="the rules must be sections like  name: ...")
        await state_set(pid, "rules", body.text)
        return {"ok": True}

    @router.delete("/plugins/{pid}/rules", dependencies=guarded)
    async def reset_rules(pid: str) -> dict:
        rules_of(pid)
        await state_set(pid, "rules", None)
        return {"ok": True}


    @router.get("/plugins", dependencies=guarded)
    async def plugins() -> dict:
        return argus.plugin_host.list()

    def plugin_or_404(pid: str):
        p = argus.plugin_host.plugins.get(pid)
        if p is None:
            raise HTTPException(status_code=404, detail=f"no plugin {pid}")
        return p

    @router.get("/plugins/{pid}", dependencies=guarded)
    async def plugin_detail(pid: str) -> dict:
        """Everything Helios's page for one plugin shows: what it is, its buttons and triggers, settings (with
        where each value comes from), Ari's tools, recent runs, time saved, learning."""
        p = plugin_or_404(pid)
        m = p.manifest
        saved = await state_get(pid, "_settings") or {}
        mine = saved.get("config") or {}
        settings = [{"name": k, "type": f.type, "label": f.label or k.replace("_", " "), "choices": f.choices,
                     "default": f.default, "value": p.config.get(k),
                     "source": "you" if k in mine else "argus.yaml" if (p.base_config or {}).get(k) != f.default
                     else "default"} for k, f in m.config.items()]
        scheds = [s for s in await argus.scheduler.list() if s["plugin"] == pid]
        runs = [job_json(j) for j in await argus.jobs.list_jobs(None, 15, pid)]
        week = await argus.store.read(lambda c: daily.time_saved(c, time.time(), 7))
        learn = await argus.store.read(lambda c: guidance.overview(c, pid))
        t = m.triggers
        return {
            "id": pid, "name": m.name, "version": m.version, "description": m.description, "runs_on": m.runs_on,
            "needs": m.job_needs(), "live": p.live,
            "live_from": "you" if saved.get("live") is not None else "argus.yaml",
            "group": m.helios.node.group, "path": str(p.path),
            "buttons": [{"workflow": x.manual.workflow, "label": x.manual.label} for x in t if x.manual],
            "schedules": [{"id": s["id"], "cron": s["cron"], "workflow": s["workflow"], "enabled": s["enabled"],
                           "next_run_at": s["next_run_at"], "last_run_at": s["last_run_at"]} for s in scheds],
            "watches": [{"workflow": x.folder_watch.workflow, "paths": x.folder_watch.paths}
                        for x in t if x.folder_watch],
            "share": [x.model_dump() for x in m.share],
            "tools": [{"name": x.name, "description": x.description, "risky": x.risky} for x in m.ari.tools],
            "rules": m.helios.rules.label if m.helios.rules else None,
            "wrong": m.helios.wrong.label if m.helios.wrong else None,
            "permissions": m.permissions.model_dump(),
            "settings": settings,
            "runs": runs,
            "saved": next((x for x in week["plugins"] if x["plugin"] == pid), {"seconds": 0, "jobs": 0}),
            "learning": learn,
        }

    @router.put("/plugins/{pid}/settings", dependencies=guarded)
    async def plugin_settings(pid: str, body: PluginSettings) -> dict:
        """Change a plugin from Helios: live or dry-run, and its settings. Takes effect with the next job."""
        from ...plugins import SettingError, check_setting

        p = plugin_or_404(pid)
        saved = {} if body.reset else dict(await state_get(pid, "_settings") or {})
        if "live" in body.model_fields_set:
            saved["live"] = body.live
        if body.config:
            mine = dict(saved.get("config") or {})
            for k, v in body.config.items():
                f = p.manifest.config.get(k)
                if f is None:
                    raise HTTPException(status_code=422, detail=f"{pid} has no setting {k!r}")
                if v is None:
                    mine.pop(k, None)
                    continue
                try:
                    mine[k] = check_setting(f, k, v)
                except SettingError as e:
                    raise HTTPException(status_code=422, detail=str(e)) from None
            saved["config"] = mine
        await state_set(pid, "_settings", saved or None)
        p.apply(saved)
        await argus.store.write(lambda c: insert_event(c, time.time(), "plugin.settings", src="helios", dst=pid,
                                                       data={"live": p.live, "changed": sorted(body.config or {})}))
        return {"live": p.live, "config": p.config}

    @router.post("/plugins/{pid}/run", dependencies=guarded)
    async def plugin_run(pid: str, body: RunPlugin) -> dict:
        """The manual trigger (a button in Helios): runs at interactive priority."""
        p = argus.plugin_host.plugins.get(pid)
        if p is None:
            raise HTTPException(status_code=404, detail=f"no plugin {pid}")
        manual = [t.manual.workflow for t in p.manifest.triggers if t.manual]
        wf = body.workflow or (manual[0] if manual else None)
        if wf is None or wf not in p.manifest.all_workflows():
            raise HTTPException(status_code=422, detail=f"{pid} has no workflow {wf!r}")
        job_id, created = await argus.jobs.enqueue(pid, wf, body.input, needs=p.manifest.job_needs(),
                                                   priority=PRIORITY_INTERACTIVE, source="helios")
        return {"id": job_id, "created": created}

    @router.get("/plugins/{pid}/state/{key}", dependencies=guarded)
    async def plugin_state_get(pid: str, key: str) -> dict:
        def fn(conn):
            row = conn.execute("SELECT value FROM plugin_state WHERE plugin = ? AND key = ?", (pid, key)).fetchone()
            return json.loads(row[0]) if row and row[0] is not None else None

        return {"value": await argus.store.read(fn)}

    @router.put("/plugins/{pid}/state/{key}", dependencies=guarded)
    async def plugin_state_put(pid: str, key: str, body: StateValue) -> dict:
        raw = json.dumps(body.value)
        if len(raw) > 256_000:
            raise HTTPException(status_code=413, detail="value too large (256 KB)")

        def fn(conn):
            now = time.time()
            conn.execute("INSERT INTO plugin_state (plugin, key, value, created_at, updated_at) VALUES (?,?,?,?,?)"
                         " ON CONFLICT(plugin, key) DO UPDATE SET value = excluded.value,"
                         " updated_at = excluded.updated_at", (pid, key[:200], raw, now, now))

        await argus.store.write(fn)
        return {"ok": True}

    return router
