"""Duplicate finder: identical files, however they are named, and which copy to keep.

`scan` (Sunday 4 am, or "Find duplicates" in Helios):

1. Lists every file in the folders you chose (config `folders`, compared together), ignoring small ones.
2. Same size -> same first 64 KB -> same full SHA-256: only then are two files duplicates. Nothing is decided by name.
3. Keeps one copy per set: the one outside Downloads, with a plain name (not "photo (1).jpg" or "Copy of ..."),
   in the shallowest folder, the oldest. The others are the extras.
4. Asks you once (a batch card in Helios and on the phone: each extra, the copy that stays, the space freed).
5. After your yes it checks each extra again (still there, still identical) and sends it to the Recycle Bin.
   Restore from the Recycle Bin if you change your mind.

In dry-run (until listed under `plugins.live`) it only reports what it found.
"""

from __future__ import annotations

import os
import re
from collections import defaultdict

from argus.worker import Context, workflow

PLUGIN = "duplicate-finder"
COPY_NAME = re.compile(r"(\(\d+\)|\bcopy\b|\bcopy of\b|[-_ ]\d{1,2}$| - copy)", re.I)


def keep_score(path: str, mtime: float, downloads: str) -> tuple:
    """Lower is better: which copy of a set stays."""
    stem = os.path.splitext(os.path.basename(path))[0]
    in_downloads = os.path.normcase(os.path.abspath(path)).startswith(os.path.normcase(downloads) + os.sep)
    return (in_downloads, bool(COPY_NAME.search(stem)), path.count(os.sep), mtime, len(path), path)


HEAD = 65536


def find_sets(ctx: Context, folders: list[str], min_size: int) -> list[dict]:
    """Sets of identical files: [{"size", "sha256", "files": [(path, mtime), ...]}]."""
    files: dict[str, tuple[str, int, float]] = {}  # one entry per real file, even if two folders overlap
    for f in folders:
        for path, size, mtime in ctx.files.walk(f):
            if size >= min_size:
                files[os.path.normcase(os.path.abspath(path))] = (path, size, mtime)
    by_size: dict[int, list[tuple[str, int, float]]] = defaultdict(list)
    for entry in files.values():
        by_size[entry[1]].append(entry)

    def split(group, key):
        out = defaultdict(list)
        for e in group:
            try:
                out[key(e)].append(e)
            except OSError:  # gone or locked meanwhile
                continue
        return [g for g in out.items() if len(g[1]) > 1]

    sets = []
    for size, same_size in by_size.items():
        if len(same_size) < 2:
            continue
        for head, same_head in split(same_size, lambda e: ctx.files.sha256(e[0], limit=HEAD)):
            groups = [(head, same_head)] if size <= HEAD else split(same_head, lambda e: ctx.files.sha256(e[0]))
            for digest, group in groups:
                sets.append({"size": size, "sha256": digest, "files": [(e[0], e[2]) for e in group]})
    return sets


def plan(ctx: Context) -> dict:
    folders = [os.path.expanduser(str(f)) for f in (ctx.config.get("folders") or ["~/Downloads"])]
    min_size = int(ctx.config.get("min_size_kb", 64)) * 1024
    downloads = os.path.normcase(os.path.abspath(os.path.expanduser("~/Downloads")))
    sets = find_sets(ctx, folders, min_size)
    out = []
    for s in sorted(sets, key=lambda s: -s["size"] * (len(s["files"]) - 1)):  # most space first
        ranked = sorted(s["files"], key=lambda f: keep_score(f[0], f[1], downloads))
        keep = ranked[0][0]
        for path, _m in ranked[1:]:
            out.append({"extra": path, "keep": keep, "size": s["size"], "sha256": s["sha256"]})
    limit = int(ctx.config.get("max_groups", 150))
    return {"extras": out[:limit], "more": max(0, len(out) - limit),
            "freed": sum(e["size"] for e in out[:limit])}


def mb(n: int) -> str:
    return f"{n / 1_048_576:.1f} MB" if n >= 1_048_576 else f"{n / 1024:.0f} KB"


def short(path: str) -> str:
    """How a path is shown to you: ~ for your home folder, forward slashes on every OS."""
    home = os.path.expanduser("~")
    s = "~" + path[len(home):] if path.startswith(home) else path
    return s.replace("\\", "/")


@workflow(PLUGIN, "scan")
def scan(ctx: Context):
    found = ctx.step("find", plan, ctx)
    extras = found["extras"]
    summary = {"sets": len({e["keep"] for e in extras}), "extras": len(extras), "space": mb(found["freed"]),
               "more_next_time": found["more"]}
    if not extras:
        return {**summary, "removed": 0, "dry_run": ctx.dry_run}
    if ctx.dry_run:
        return {**summary, "would_remove": [short(e["extra"]) for e in extras[:50]], "dry_run": True}

    def ask():
        d = ctx.approve("batch", f"Remove {len(extras)} duplicate files ({mb(found['freed'])})",
                        items=[{"label": short(e["extra"]), "keeps": short(e["keep"]), "size": mb(e["size"])}
                               for e in extras],
                        summary=[f"{summary['sets']} sets of identical files; the copy that stays is listed with "
                                 "each one.", "They go to the Recycle Bin (you can restore them)."])
        return {"approved": bool(d)}

    if not ctx.step("ask", ask)["approved"]:
        return {**summary, "removed": 0, "rejected": True}

    def remove():
        gone, skipped, freed = 0, [], 0
        for e in extras:
            try:  # still there and still identical to the one we keep? (things change while you decide)
                if not ctx.files.exists(e["keep"]) or not ctx.files.exists(e["extra"]):
                    skipped.append(short(e["extra"]))
                    continue
                if ctx.files.sha256(e["extra"]) != ctx.files.sha256(e["keep"]):
                    skipped.append(short(e["extra"]))
                    continue
                ctx.files.recycle(e["extra"])
                gone += 1
                freed += e["size"]
            except OSError:
                skipped.append(short(e["extra"]))
        return {"removed": gone, "freed": mb(freed), "skipped": skipped}

    done = ctx.step("remove", remove)
    ctx.emit("removed", count=done["removed"], freed=done["freed"])
    ctx.saved(60 + 10 * done["removed"], key="removed")  # comparing files by hand: a minute, and ~10 s each
    return {**summary, **done}
