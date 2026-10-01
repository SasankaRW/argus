"""Everything search: Ari asks Everything (voidtools) through its command line, es.exe, and gets paths back in
milliseconds, for every drive. Names, sizes and dates only; nothing is opened or read here. Folders blocked in
argus.yaml (paths.blocked) are taken out of the results.

Setup on the PC: Everything installed and running; es.exe (voidtools.com/downloads, "Command-line Interface") in
the Everything folder or on PATH.
"""

from __future__ import annotations

import csv
import io
import ntpath
import os
import platform
import shutil
import subprocess
import time

from argus.worker import Context, PermanentError, workflow

PLUGIN = "everything"
WIN = platform.system() == "Windows"
PLACES = [r"C:\Program Files\Everything\es.exe", r"C:\Program Files (x86)\Everything\es.exe"]


def find_es(configured: str) -> str:
    if configured:
        if os.path.isfile(configured):
            return configured
        raise PermanentError(f"es.exe not found at {configured}")
    hit = shutil.which("es.exe") or shutil.which("es")
    if hit:
        return hit
    for c in PLACES:
        if os.path.isfile(c):
            return c
    raise PermanentError("es.exe not found: get Everything's command line (voidtools.com/downloads) and put es.exe in "
                         "the Everything folder")


def args(es: str, query: str, n: int) -> list[str]:
    return [es, "-n", str(n), "-sort", "date-modified-descending", "-size", "-date-modified", "-csv", query]


def parse(out: str) -> list[dict]:
    """es.exe -csv: a header row (Filename, Size, Date Modified), then one row per file."""
    rows = list(csv.reader(io.StringIO(out)))
    if not rows:
        return []
    head = [h.strip().lower() for h in rows[0]]
    if "filename" not in head:
        head, body = ["filename", "size", "date modified"][:len(rows[0])], rows
    else:
        body = rows[1:]
    found = []
    for r in body:
        d = dict(zip(head, r))  # noqa: B905 - short rows are fine
        path = d.get("filename", "").strip()
        if not path:
            continue
        size = d.get("size", "").strip()
        found.append({"path": path, "name": ntpath.basename(path.rstrip("\\/")) or path,
                      "size_kb": round(int(size) / 1024) if size.isdigit() else None,
                      "modified": d.get("date modified", "").strip() or None})
    return found


@workflow(PLUGIN, "search")
def search(ctx: Context):
    query = " ".join(str(ctx.input.get("query") or "").split())[:300]
    if not query:
        raise PermanentError("search for what?")
    n = max(1, min(100, int(ctx.config.get("results", 20))))

    def go():
        if not WIN:
            raise PermanentError("Everything runs on Windows only")
        es = find_es(str(ctx.config.get("es_path") or "").strip())
        t0 = time.monotonic()
        try:
            p = subprocess.run(args(es, query, n * 2), capture_output=True, text=True, timeout=20,
                               encoding="utf-8", errors="replace",
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired) as e:
            raise PermanentError(f"es.exe: {e}") from None
        if p.returncode != 0:
            msg = (p.stderr or p.stdout).strip()
            if "IPC" in msg or "not running" in msg.lower() or p.returncode == 8:
                raise PermanentError("Everything isn't running (start it, or set it to start with Windows)")
            raise PermanentError(f"es.exe: {msg[:200] or f'exit {p.returncode}'}")
        found = [f for f in parse(p.stdout) if ctx.files.visible(f["path"])]
        return {"query": query, "files": found[:n], "more": max(0, len(found) - n),
                "ms": round((time.monotonic() - t0) * 1000)}

    return ctx.step("search", go)
