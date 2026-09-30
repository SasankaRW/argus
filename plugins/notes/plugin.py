"""Notes: "note: buy printer ink" -> ~/Documents/notes/2026-09-30.md gets "- 21:40 buy printer ink".

One markdown file per day, only ever added to (ctx.files.append_text), so nothing you wrote there is changed. The
knowledge plugin indexes Documents, so Ari's file search finds notes too; find_notes is the quick exact look.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

from argus.worker import Context, PermanentError, workflow

PLUGIN = "notes"
DAY = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")
LINE = re.compile(r"^- (\d{2}:\d{2}) (.*)$")


def folder(ctx: Context) -> Path:
    return Path(os.path.expanduser(str(ctx.config.get("folder") or "~/Documents/notes")))


def entry(text: str, now: float) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^(note|notes|write down|jot down)\s*[:\-]\s*", "", text, flags=re.I)
    if not text:
        raise PermanentError("the note is empty")
    return f"- {time.strftime('%H:%M', time.localtime(now))} {text}\n"


def read_notes(ctx: Context, since: str = "") -> list[dict]:
    """Every note in the folder (newest first); files named YYYY-MM-DD.md, dates >= since."""
    out = []
    d = folder(ctx)
    if not ctx.files.is_dir(d):
        return []
    for path in reversed(ctx.files.list(d, "*.md")):
        m = DAY.match(os.path.basename(path))
        if not m or m.group(1) < since:
            continue
        lines = ctx.files.read_text(path).splitlines()
        for ln in reversed(lines):
            n = LINE.match(ln.strip())
            if n:
                out.append({"date": m.group(1), "time": n.group(1), "note": n.group(2)})
            elif ln.strip() and not ln.startswith("#"):
                out.append({"date": m.group(1), "time": None, "note": ln.strip().lstrip("-* ")})
    return out


@workflow(PLUGIN, "add")
def add(ctx: Context):
    def write():
        now = time.time()
        line = entry(str(ctx.input.get("text") or ""), now)
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        path = folder(ctx) / f"{day}.md"
        head = "" if ctx.files.exists(path) else f"# Notes {day}\n\n"
        where = ctx.files.append_text(path, head + line)
        return {"added": line.strip()[2:], "file": where, "dry_run": ctx.dry_run}

    out = ctx.step("add", write)
    ctx.saved(20, key="note")
    return out


@workflow(PLUGIN, "find")
def find(ctx: Context):
    words = [w for w in re.split(r"\W+", str(ctx.input.get("query") or "").lower()) if w]
    if not words:
        raise PermanentError("what should I look for?")

    def look():
        hits = [n for n in read_notes(ctx) if all(w in n["note"].lower() for w in words)]
        return {"notes": hits[:20], "more": max(0, len(hits) - 20)}

    return ctx.step("find", look)


@workflow(PLUGIN, "recent")
def recent(ctx: Context):
    days = max(1, min(int(ctx.input.get("days") or 7), 90))
    since = time.strftime("%Y-%m-%d", time.localtime(time.time() - (days - 1) * 86400))
    return ctx.step("read", lambda: {"notes": read_notes(ctx, since)[:50], "days": days})
