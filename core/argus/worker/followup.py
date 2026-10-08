"""The guidance loop, part 5: did you fix what a job did? (guidance.followup)

argusd sends the recent moves and renames that plugins made whose model answers have no verdict yet. For each, this
looks (names and sizes only, never the contents):

- still where the job put it: "there" (left alone for a day, the answer counts as a yes and becomes a worked
  example);
- back where it came from: "moved back";
- same size and time, another name in the same folder: "renamed";
- same size and time somewhere near: "moved";
- not found: "gone" (deleted or far away; nothing is concluded).

The results go back with the record_followups tool; argusd turns your fixes into wrong answers with what you did.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .workflows import Context, workflow

MAX_SCAN = 4000  # entries looked at per item, at most


def _same(st: os.stat_result, size: Any, mtime: Any) -> bool:
    return size is not None and st.st_size == int(size) and (mtime is None or abs(st.st_mtime - float(mtime)) < 2.5)


def find(to: str, frm: str, size: Any, mtime: Any) -> tuple[str, str | None]:
    """-> (status, where it is now)."""
    if os.path.exists(to):
        return "there", to
    if frm and os.path.exists(frm):
        try:
            if size is None or _same(os.stat(frm), size, mtime):
                return "moved back", frm
        except OSError:
            pass
    if size is None:
        return "gone", None
    here = Path(to).parent
    roots = [here, Path(frm).parent if frm else None, here.parent]
    seen, looked = set(), 0
    for root in roots:
        if root is None or root in seen or not root.is_dir():
            continue
        seen.add(root)
        for dirpath, dirs, files in os.walk(root):
            depth = len(Path(dirpath).relative_to(root).parts)
            if depth >= 2:
                dirs[:] = []
            for f in files:
                looked += 1
                if looked > MAX_SCAN:
                    return "gone", None
                p = os.path.join(dirpath, f)
                try:
                    if _same(os.stat(p), size, mtime):
                        same_dir = os.path.normcase(dirpath) == os.path.normcase(str(here))
                        return ("renamed" if same_dir else "moved"), p
                except OSError:
                    continue
    return "gone", None


@workflow("guidance", "followup")
def followup(ctx: Context):
    items = list(ctx.input.get("items") or [])

    def look() -> list[dict[str, Any]]:
        out = []
        for x in items:
            status, now_at = find(str(x.get("to") or ""), str(x.get("from") or ""), x.get("size"), x.get("mtime"))
            out.append({"job_id": x.get("job_id"), "event_id": x.get("event_id"), "from": x.get("from"),
                        "status": status, "now_at": now_at, "age": x.get("age")})
        return out

    results = ctx.step("look", look)
    counts = ctx.step("record", lambda: ctx.tool("record_followups", {"results": results}))
    seen: dict[str, int] = {}
    for r in results:
        seen[r["status"]] = seen.get(r["status"], 0) + 1
    return {"looked": len(results), "seen": seen, "recorded": counts}
