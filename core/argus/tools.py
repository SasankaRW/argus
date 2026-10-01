"""Tools: what Ari (and Claude through the MCP server) can use.

Two kinds:
- built in: Argus's own data (status, jobs, schedules, time saved, the phone), answered by argusd at once;
- plugin tools: declared in a plugin's plugin.yaml (`ari: tools:`), run as a job of that plugin's workflow with the
  tool's arguments as the job's input, on a worker that can run it (so the plugin's permissions apply, and PC tools
  run in your Windows session).

A tool marked `risky` (closing apps, typing, changing files) is never run by Ari without your yes.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from . import ask as ask_mod
from . import daily, logview
from .config import PRIORITY_INTERACTIVE


class ToolError(Exception):
    pass


@dataclass
class Tool:
    name: str
    description: str
    props: dict[str, Any] = field(default_factory=dict)
    required: list[str] = field(default_factory=list)
    risky: bool = False
    private: bool = False  # its results never go to Claude (Ari's thinking stays local; not offered over MCP)
    untrusted: bool = False  # it returns outside text (web pages): after it, Ari asks before doing anything
    plugin: str | None = None  # a plugin tool: runs as a job of plugin.workflow
    workflow: str | None = None
    fn: Callable[[dict[str, Any]], Awaitable[Any]] | None = None  # a built-in tool
    for_ari: bool = True
    for_mcp: bool = True

    def schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": self.props, "required": self.required, "additionalProperties": False}

    def brief(self) -> dict[str, Any]:
        """What the model sees."""
        args = {k: v.get("description") or v.get("type", "") for k, v in self.props.items()}
        return {"name": self.name, "does": self.description, "args": args, "required": self.required,
                **({"asks_first": True} if self.risky else {}), **({"private": True} if self.private else {}),
                **({"untrusted": True} if self.untrusted else {})}


def check_args(tool: Tool, args: Any) -> dict[str, Any]:
    if not isinstance(args, dict):
        raise ToolError("arguments must be an object")
    missing = [r for r in tool.required if r not in args or args[r] in (None, "")]
    if missing:
        raise ToolError(f"{tool.name} needs {', '.join(missing)}")
    unknown = [k for k in args if k not in tool.props]
    if unknown:
        raise ToolError(f"{tool.name} has no argument {', '.join(unknown)}")
    return args


class Tools:
    def __init__(self, argus, job_json: Callable[..., dict]):
        self.argus = argus
        self.job_json = job_json
        self.builtin: dict[str, Tool] = {t.name: t for t in self._builtins()}

    # -------------------------------------------------------------- the catalogue

    def all(self) -> dict[str, Tool]:
        out = dict(self.builtin)
        for pid, p in sorted(self.argus.plugin_host.plugins.items()):
            for t in p.manifest.ari.tools:
                out.setdefault(t.name, Tool(t.name, t.description, dict(t.input), list(t.required), t.risky,
                                            plugin=pid, workflow=t.workflow, private=t.private, untrusted=t.untrusted,
                                            for_mcp=not t.private))
        return out

    def get(self, name: str) -> Tool:
        t = self.all().get(name)
        if t is None:
            raise ToolError(f"no tool {name!r}")
        return t

    # -------------------------------------------------------------- running a tool outside a job (Ari's "yes", MCP)

    async def run_now(self, name: str, args: dict[str, Any], wait: float = 45) -> Any:
        tool = self.get(name)
        args = check_args(tool, args or {})
        if tool.fn is not None:
            return await tool.fn(args)
        job_id, _ = await self.enqueue(tool, args, parent=None, key=None)
        end = time.monotonic() + wait
        while time.monotonic() < end:
            j = await self.argus.jobs.get(job_id)
            if j.state.value == "succeeded":
                return j.result
            if j.state.value in ("dead", "cancelled"):
                raise ToolError((j.error or "failed").split("\n")[0])
            await asyncio.sleep(0.3)
        return {"started": job_id, "note": "still running; see Helios > Runs"}

    async def enqueue(self, tool: Tool, args: dict[str, Any], *, parent: str | None, key: str | None
                      ) -> tuple[str, bool]:
        p = self.argus.plugin_host.plugins.get(tool.plugin)
        if p is None:
            raise ToolError(f"plugin {tool.plugin} is not loaded")
        return await self.argus.jobs.enqueue(
            tool.plugin, tool.workflow, {**args, **({"_parent": parent} if parent else {})},
            needs=p.manifest.job_needs(), priority=PRIORITY_INTERACTIVE, source="ari",
            dedupe_key=f"tool:{key}" if key else None)

    # -------------------------------------------------------------- a tool called from inside a job (ctx.tool)

    async def call_from_job(self, parent_id: str, worker: str, key: str, name: str, args: dict[str, Any]
                            ) -> dict[str, Any]:
        """Built in: the answer now. Plugin tool: a child job; "pending" until it ends (the parent then waits and
        is resumed when the child is done)."""
        await self.argus.jobs.check_holder(parent_id, worker)
        try:
            tool = self.get(name)
            args = check_args(tool, args or {})
        except ToolError as e:
            return {"state": "failed", "error": str(e)}
        if tool.fn is not None:
            try:
                return {"state": "done", "result": await tool.fn(args)}
            except ToolError as e:
                return {"state": "failed", "error": str(e)}
            except Exception as e:  # an HTTPException from a helper, a bad argument type
                return {"state": "failed", "error": str(getattr(e, "detail", None) or e)[:300]}
        child = await self.argus.store.read(lambda c: c.execute(
            "SELECT id, state, result, error FROM jobs WHERE dedupe_key = ? ORDER BY created_at DESC LIMIT 1",
            (f"tool:{key}",)).fetchone())
        if child is None:
            try:
                cid, _ = await self.enqueue(tool, args, parent=parent_id, key=key)
            except ToolError as e:
                return {"state": "failed", "error": str(e)}
            return {"state": "pending", "job": cid}
        if child["state"] == "succeeded":
            return {"state": "done", "result": json.loads(child["result"]) if child["result"] else None}
        if child["state"] in ("dead", "cancelled"):
            return {"state": "failed", "error": (child["error"] or "failed").split("\n")[0][:300]}
        return {"state": "pending", "job": child["id"]}

    # -------------------------------------------------------------- built-in tools

    def _builtins(self) -> list[Tool]:
        a = self.argus

        async def status(_: dict) -> Any:
            q = await a.jobs.queue(await a.registry.workers())
            h = a.health()
            return {"status": h["status"], "version": h.get("version"), "workers_online": q["workers_online"],
                    "running": [f"{j['plugin']}.{j['workflow']}" for j in q["running"]],
                    "queued": [f"{j['plugin']}.{j['workflow']} ({j.get('why')})" for j in q["queued"]][:20],
                    "waiting": [f"{j['plugin']}.{j['workflow']}" for j in q["waiting"]],
                    "power": a.power.status()}

        async def list_jobs(x: dict) -> Any:
            from .jobs import JobState

            try:
                state = JobState(x["state"]) if x.get("state") else None
            except ValueError:
                raise ToolError(f"no job state {x['state']!r}") from None
            jobs = await a.jobs.list_jobs(state, min(int(x.get("limit") or 20), 100), x.get("plugin"))
            return [{"id": j.id, "job": f"{j.plugin}.{j.workflow}", "state": j.state.value,
                     "created": time.strftime("%Y-%m-%d %H:%M", time.localtime(j.created_at)),
                     "error": (j.error or "").split("\n")[0][:200] or None} for j in jobs]

        async def get_job(x: dict) -> Any:
            try:
                return self.job_json(await a.jobs.get(str(x["id"])), await a.jobs.steps(str(x["id"])))
            except Exception:
                raise ToolError(f"no job {x['id']!r}") from None

        async def read_log(x: dict) -> Any:
            path = logview.sources(a.cfg.log_dir).get(str(x["name"]))
            if path is None:
                raise ToolError(f"no log {x['name']!r}; there are: {', '.join(logview.sources(a.cfg.log_dir))}")
            n = min(int(x.get("lines") or 100), 500)
            out = await asyncio.to_thread(logview.read, path, str(x["name"]), lines=n)
            return out["entries"]

        async def approvals(_: dict) -> Any:
            return [{"id": r["id"], "plugin": r["plugin"], "title": r["title"], "type": r["type"]}
                    for r in await a.approvals.list("pending", None, 100)]

        async def schedules(_: dict) -> Any:
            return [{"id": s["id"], "what": s.get("label") or f"{s['plugin']}.{s['workflow']}", "cron": s["cron"],
                     "enabled": s["enabled"], "owner": s.get("owner"),
                     "next": time.strftime("%Y-%m-%d %H:%M", time.localtime(s["next_run_at"]))
                     if s.get("next_run_at") else None} for s in await a.scheduler.list()]

        async def saved(x: dict) -> Any:
            return await a.store.read(lambda c: daily.time_saved(c, time.time(), min(int(x.get("days") or 7), 366)))

        async def weather(a: dict) -> Any:
            said = await self.argus.weather_say(str(a.get("place") or ""), 1 if a.get("tomorrow") else 0)
            if said is None:
                raise ToolError("no town set (Settings > Weather in the brief) or no connection")
            return {"forecast": said, "source": "Open-Meteo"}

        async def week(_: dict) -> Any:
            labels = a.button_labels()
            return await a.store.read(lambda c: daily.week_review(c, time.time(), labels))

        async def buttons(_: dict) -> Any:
            return [b for b in ask_mod.catalog(a.plugin_host) if b["id"].startswith("run:")]

        async def run_button(x: dict) -> Any:
            bid = str(x["id"])
            if not bid.startswith("run:"):
                raise ToolError("only plugin buttons (run:...) can be run; see list_buttons")
            _, pid, wf = (bid.split(":", 2) + ["", ""])[:3]
            p = a.plugin_host.plugins.get(pid)
            if p is None or wf not in [t.manual.workflow for t in p.manifest.triggers if t.manual]:
                raise ToolError(f"no button {bid!r}")
            job_id, created = await a.jobs.enqueue(pid, wf, {}, needs=p.manifest.job_needs(),
                                                   priority=PRIORITY_INTERACTIVE, source="ari")
            return {"job_id": job_id, "created": created}

        async def ask_argus(x: dict) -> Any:
            actions = ask_mod.catalog(a.plugin_host)
            hit = ask_mod.rules(str(x["text"]), actions, await ask_mod.snapshot(a))
            return hit or {"reply": "No instant answer; ask about the queue, failures, approvals, or a button.",
                           "action": None}

        async def backups(_: dict) -> Any:
            files = a.backups.list()
            newest = files[0]["made_at"] if files else None
            return {"enabled": a.cfg.backup.enabled, "count": len(files), "newest": files[0]["file"] if files else None,
                    "hours_ago": round((time.time() - newest) / 3600, 1) if newest else None,
                    "copy_to": a.cfg.backup.copy_to}

        return [
            Tool("backup_status", "Argus's own backups: how many, the newest and how many hours ago it was made.",
                 fn=backups),
            Tool("argus_status", "Argus right now: health, workers, what runs, what is queued, what waits for the "
                 "user, the PC's power state.", fn=status),
            Tool("list_jobs", "Recent jobs, newest first (optionally only one state or one plugin).",
                 {"state": {"type": "string", "description": "queued, running, waiting, succeeded, dead, ..."},
                  "plugin": {"type": "string", "description": "a plugin id"},
                  "limit": {"type": "integer", "description": "how many (max 100)"}}, fn=list_jobs),
            Tool("get_job", "One job in detail: input, steps (tier used, errors), result.",
                 {"id": {"type": "string", "description": "the job id"}}, ["id"], fn=get_job),
            Tool("read_log", "The last lines of an Argus log (argus, worker, supervisor, ollama, ari, ...).",
                 {"name": {"type": "string", "description": "the log's name"},
                  "lines": {"type": "integer", "description": "how many lines (max 500)"}}, ["name"], fn=read_log,
                 for_ari=False),
            Tool("list_approvals", "What waits for the user's decision (only the user can approve).", fn=approvals),
            Tool("list_schedules", "The schedules: from settings, plugins and the ones the user made with Ari.",
                 fn=schedules),
            Tool("time_saved", "Time the plugins saved the user over the last days.",
                 {"days": {"type": "integer", "description": "how many days back (default 7)"}}, fn=saved),
            Tool("weekly_review", "The user's week with Argus: jobs done, failures, time saved, and things they keep "
                 "doing by hand that could be scheduled (with what to say to schedule them).", fn=week),
            Tool("weather", "The weather today or tomorrow for a town (default: the user's town).",
                 {"place": {"type": "string", "description": "a town, e.g. Kandy (empty: the user's town)"},
                  "tomorrow": {"type": "boolean", "description": "true for tomorrow"}}, fn=weather),
            Tool("list_buttons", "The plugin buttons Argus can run (ids for run_button).", fn=buttons),
            Tool("run_button", "Run a plugin's button now, e.g. run:downloads-organizer:sort (sort Downloads).",
                 {"id": {"type": "string", "description": "the button id from list_buttons"}}, ["id"], risky=True,
                 fn=run_button),
            Tool("ask_argus", "Argus's instant answers (queue, failures, approvals, buttons).",
                 {"text": {"type": "string", "description": "the question"}}, ["text"], fn=ask_argus, for_ari=False),
        ]
