"""Dev helper: git (and gh, when there) on each repo under the configured folders. Reads only.

A repo is a folder with a .git inside, one level below a configured folder (G:/Projects/argus), or the folder itself.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from typing import Any

from argus.worker import Context, workflow

PLUGIN = "devhelp"


def git(repo: str, *args: str, timeout: float = 15) -> str | None:
    exe = shutil.which("git")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "-C", repo, *args], capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def find_repos(folders: list[str]) -> list[str]:
    out = []
    for f in folders:
        root = os.path.expanduser(f)
        if os.path.isdir(os.path.join(root, ".git")):
            out.append(root)
            continue
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        out += [os.path.join(root, n) for n in names if os.path.isdir(os.path.join(root, n, ".git"))]
    return out


def parse_ahead_behind(text: str | None) -> tuple[int, int] | None:
    """`git rev-list --left-right --count @{u}...HEAD` -> (ahead, behind)."""
    if not text:
        return None
    p = text.split()
    if len(p) != 2:
        return None
    return int(p[1]), int(p[0])


def ci(repo: str) -> dict | None:
    exe = shutil.which("gh")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "run", "list", "-L", "1", "--json", "status,conclusion,name,headBranch,createdAt"],
                           cwd=repo, capture_output=True, text=True, timeout=20)
        runs = json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else []
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    if not runs:
        return None
    x = runs[0]
    return {"workflow": x.get("name"), "branch": x.get("headBranch"),
            "result": x.get("conclusion") or x.get("status"), "at": (x.get("createdAt") or "")[:16].replace("T", " ")}


def repo_info(repo: str, with_ci: bool) -> dict[str, Any]:
    status = git(repo, "status", "--porcelain") or ""
    changed = [ln[3:] for ln in status.splitlines() if ln.strip()]
    last = (git(repo, "log", "-1", "--format=%h|%s|%cr") or "").split("|", 2)
    ab = parse_ahead_behind(git(repo, "rev-list", "--left-right", "--count", "@{u}...HEAD"))
    info: dict[str, Any] = {
        "repo": os.path.basename(repo.rstrip("/\\")), "branch": git(repo, "branch", "--show-current") or "(detached)",
        "uncommitted": len(changed), "uncommitted_files": changed[:10],
        "last_commit": {"id": last[0], "message": last[1], "when": last[2]} if len(last) == 3 else None,
    }
    if ab is None:
        info["upstream"] = "none (not pushed yet)"
    else:
        info["ahead"], info["behind"] = ab
    if with_ci:
        info["ci"] = ci(repo)
    return info


def folders(ctx: Context) -> list[str]:
    return [str(f) for f in (ctx.config.get("folders") or ["G:/Projects"])]


@workflow(PLUGIN, "repos")
def repos(ctx: Context):
    name = str(ctx.input.get("name") or "").strip().lower()

    def look():
        rs = find_repos(folders(ctx))
        if name:
            rs = [r for r in rs if name in os.path.basename(r).lower()]
        with_ci = bool(ctx.config.get("ci", True)) and len(rs) <= 8
        return {"repos": [repo_info(r, with_ci) for r in rs[:30]],
                "note": None if rs else f"no git repos in {', '.join(folders(ctx))}" + (f" named {name!r}" if name
                                                                                       else "")}

    return ctx.step("look", look)


@workflow(PLUGIN, "today")
def today(ctx: Context):
    def look():
        midnight = time.strftime("%Y-%m-%d 00:00")
        out = []
        for r in find_repos(folders(ctx)):
            log = git(r, "log", f"--since={midnight}", "--all", "--format=%h|%s|%ad", "--date=format:%H:%M") or ""
            commits = [dict(zip(("id", "message", "time"), ln.split("|", 2), strict=False)) for ln in log.splitlines()
                       if ln]
            dirty = len([ln for ln in (git(r, "status", "--porcelain") or "").splitlines() if ln.strip()])
            if commits or dirty:
                out.append({"repo": os.path.basename(r), "commits": commits, "uncommitted": dirty})
        return {"today": out, "note": None if out else "nothing changed in your repos today"}

    return ctx.step("look", look)
