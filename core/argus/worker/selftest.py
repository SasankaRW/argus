"""Ari's tools, tested (Helios > Settings > "Test Ari's tools"): each read-only tool runs once, the way Ari would
use it, and the answer says which work.

Only tools that look and change nothing are run: nothing opens, closes, types, copies or moves. "deep" adds the
slow ones (looking at the screen with the vision model).
"""

from __future__ import annotations

from typing import Any

from .workflows import Context, ToolFailed, workflow

# tool -> the arguments it is tested with. Read-only only.
SAFE: dict[str, dict[str, Any]] = {
    "argus_status": {},
    "pc_status": {},
    "list_apps": {},
    "read_clipboard": {},
    "recent_notes": {"days": 1},
    "list_routines": {},
    "lab_status": {},
    "repo_status": {},
    "list_watches": {},
    "find_file": {"name": "readme"},
    "search_my_files": {"query": "argus"},
}
DEEP: dict[str, dict[str, Any]] = {"look_at_screen": {"question": "Which app is in front? One sentence."}}


def summary(result: Any) -> str:
    """A few words about what came back, never the content itself (the clipboard, your notes)."""
    if isinstance(result, dict):
        if result.get("dry_run"):
            return "dry-run"
        for k, v in result.items():
            if isinstance(v, list):
                return f"{len(v)} {k[:-1] if len(v) == 1 and k.endswith('s') else k}"
        return ", ".join(list(result)[:4]) or "empty"
    if isinstance(result, list):
        return f"{len(result)} items"
    return type(result).__name__


@workflow("ari", "selftest")
def selftest(ctx: Context):
    have = {t["name"]: t for t in ctx.input.get("tools") or []}
    plan = {**SAFE, **(DEEP if ctx.input.get("deep") else {})}
    out = []
    for name, args in plan.items():
        if name not in have:
            continue
        args = {k: v for k, v in args.items() if k in (have[name].get("args") or {})}

        def run(name=name, args=args) -> dict:
            try:
                r = ctx.tool(name, args)
                return {"tool": name, "plugin": have[name].get("plugin"), "ok": True, "about": summary(r)}
            except ToolFailed as e:
                return {"tool": name, "plugin": have[name].get("plugin"), "ok": False, "error": str(e)[:240]}

        out.append(ctx.step(f"test {name}", run))
    bad = [r["tool"] for r in out if not r["ok"]]
    return {"tested": len(out), "failed": bad, "results": out,
            "say": f"All {len(out)} tools work." if not bad else f"{len(bad)} of {len(out)} failed: {', '.join(bad)}."}
