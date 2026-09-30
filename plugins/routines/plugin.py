"""Routines: named chains of Ari's tools, kept in the plugin's store in argusd.

"work mode" = open_app name=Visual Studio Code; open_app name=Google Chrome; set_volume level=20

Saving, replacing or deleting a routine is a risky tool (Ari asks first and shows the steps); running a saved one
doesn't ask again, since you approved its steps when it was saved. Each step is ctx.tool(): a plugin's tool runs as
its own job (with that plugin's permissions), so a routine can do nothing its tools couldn't do on their own.
"""

from __future__ import annotations

import re
from typing import Any

from argus.worker import Context, PermanentError, ToolFailed, workflow

PLUGIN = "routines"
MAX_STEPS = 20
DEFAULTS = {
    "work mode": "open_app name=Visual Studio Code; open_app name=Google Chrome; set_volume level=20",
}


def key(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


def parse_steps(text: str) -> list[dict[str, Any]]:
    """'open_app name=Visual Studio Code; set_volume level=20' -> [{tool, args}, ...]. Values run until the next
    ' word=' so they may hold spaces; whole numbers become numbers."""
    steps = []
    for part in re.split(r"[;\n]+", text or ""):
        part = part.strip().strip("-•*").strip()
        if not part:
            continue
        m = re.match(r"([a-z][a-z0-9_]*)\s*(.*)$", part)
        if not m:
            raise PermanentError(f"not a step: {part!r} (write 'tool_name arg=value')")
        tool, rest = m.group(1), m.group(2).strip()
        args: dict[str, Any] = {}
        if rest:
            pieces = re.split(r"\s+(?=[a-z_][a-z0-9_]*=)", rest)
            for p in pieces:
                if "=" not in p:
                    raise PermanentError(f"in {part!r}: {p!r} needs a name, like name={p}")
                k, v = p.split("=", 1)
                v = v.strip().strip('"').strip("'")
                args[k.strip()] = int(v) if re.fullmatch(r"-?\d+", v) else v
        steps.append({"tool": tool, "args": args})
    if not steps:
        raise PermanentError("a routine needs at least one step")
    if len(steps) > MAX_STEPS:
        raise PermanentError(f"a routine can have at most {MAX_STEPS} steps")
    if any(s["tool"] in ("run_routine", "save_routine", "delete_routine") for s in steps):
        raise PermanentError("a routine can't run or change routines")
    return steps


def show(steps: list[dict[str, Any]]) -> str:
    return "; ".join(s["tool"] + "".join(f" {k}={v}" for k, v in s["args"].items()) for s in steps)


def load_all(ctx: Context) -> dict[str, dict[str, Any]]:
    saved = ctx.store.get("routines") if ctx.store else None
    if saved is None:
        return {k: {"name": k, "steps": parse_steps(v)} for k, v in DEFAULTS.items()}
    return saved


def find(routines: dict[str, dict[str, Any]], name: str) -> dict[str, Any]:
    k = key(name)
    if k in routines:
        return routines[k]
    close = [r for n, r in routines.items() if k in n or n in k]
    if len(close) == 1:
        return close[0]
    names = ", ".join(sorted(r["name"] for r in routines.values())) or "none yet"
    raise PermanentError(f"no routine called {name!r} (yours: {names})")


@workflow(PLUGIN, "list")
def list_routines(ctx: Context):
    rs = ctx.step("read", lambda: load_all(ctx))
    return {"routines": [{"name": r["name"], "steps": show(r["steps"])} for r in rs.values()]}


@workflow(PLUGIN, "run")
def run(ctx: Context):
    r = ctx.step("find", lambda: find(load_all(ctx), str(ctx.input.get("name") or "")))
    done = []
    for i, s in enumerate(r["steps"], 1):
        label = f"{i}. {s['tool']}"
        if ctx.dry_run:
            done.append({"step": label, "would": s["args"]})
            continue
        try:
            out = ctx.step(label, lambda s=s: ctx.tool(s["tool"], s["args"]))
        except ToolFailed as e:
            return {"routine": r["name"], "done": done, "stopped_at": label, "error": str(e)[:300]}
        done.append({"step": label, "result": out if isinstance(out, (str, int, float, bool)) or out is None
                     else "ok"})
    ctx.saved(10 * len(done), key="run")
    return {"routine": r["name"], "done": done, "dry_run": ctx.dry_run}


@workflow(PLUGIN, "save")
def save(ctx: Context):
    name = str(ctx.input.get("name") or "").strip()
    if not name:
        raise PermanentError("the routine needs a name")
    steps = parse_steps(str(ctx.input.get("steps") or ""))

    def write():
        rs = load_all(ctx)
        replaced = key(name) in rs
        rs[key(name)] = {"name": name, "steps": steps}
        if not ctx.dry_run:
            ctx.store.set("routines", rs)
        return {"saved": name, "steps": show(steps), "replaced": replaced, "dry_run": ctx.dry_run}

    return ctx.step("save", write)


@workflow(PLUGIN, "delete")
def delete(ctx: Context):
    def rm():
        rs = load_all(ctx)
        r = find(rs, str(ctx.input.get("name") or ""))
        del rs[key(r["name"])]
        if not ctx.dry_run:
            ctx.store.set("routines", rs)
        return {"deleted": r["name"], "dry_run": ctx.dry_run}

    return ctx.step("delete", rm)
