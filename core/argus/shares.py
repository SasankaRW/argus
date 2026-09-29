"""Things sent from the phone's share menu (Helios > Share): photos, PDFs, links, text.

Helios uploads them here first (`POST /shares`, then `PUT /shares/{id}/files`), then `POST /shares/{id}/send`
queues a job for the plugin you picked. The job's worker fetches the files over HTTP (`ctx.shared(name)`), so it
may run on another machine. Shares are kept in `<data>/shares/<id>/` and deleted after `share.keep_days`.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path
from typing import Any

from .ids import new_id

SAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
ID = re.compile(r"^[0-9A-Z]{10,40}$")


class ShareError(Exception):
    pass


def safe_name(name: str) -> str:
    n = SAFE.sub("_", name).strip(" .")[:150]
    return n or "file"


def kinds_of(meta: dict[str, Any]) -> set[str]:
    """What a share holds, for matching plugins' `accepts`."""
    k: set[str] = set()
    for f in meta.get("files", []):
        k.add("file")
        t = f.get("type") or ""
        if t.startswith("image/"):
            k.add("image")
        if t == "application/pdf" or f["name"].lower().endswith(".pdf"):
            k.add("pdf")
    if meta.get("url"):
        k.add("url")
    if meta.get("text"):
        k.add("text")
    return k


class ShareStore:
    def __init__(self, root: Path, max_mb: float, keep_days: float, clock=time.time):
        self.root = root
        self.max_bytes = int(max_mb * 1024 * 1024)
        self.keep = keep_days * 86400
        self.clock = clock

    def _dir(self, sid: str) -> Path:
        if not ID.match(sid):
            raise ShareError("bad share id")
        d = self.root / sid
        if not (d / "meta.json").exists():
            raise ShareError("no such share")
        return d

    def meta(self, sid: str) -> dict[str, Any]:
        return json.loads((self._dir(sid) / "meta.json").read_text(encoding="utf-8"))

    def _save(self, sid: str, meta: dict[str, Any]) -> None:
        (self.root / sid / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    def create(self, title: str = "", text: str = "", url: str = "", note: str = "") -> dict[str, Any]:
        self.cleanup()
        sid = new_id()
        (self.root / sid).mkdir(parents=True)
        meta = {"id": sid, "created_at": self.clock(), "title": title[:300], "text": text[:20000],
                "url": url[:2000], "note": note[:2000], "files": [], "job_id": None}
        self._save(sid, meta)
        return meta

    def add_file(self, sid: str, name: str, mime: str, data: bytes) -> dict[str, Any]:
        meta = self.meta(sid)
        if meta["job_id"]:
            raise ShareError("already sent")
        used = sum(f["size"] for f in meta["files"])
        if used + len(data) > self.max_bytes:
            raise ShareError(f"too big: shares are limited to {self.max_bytes // (1024 * 1024)} MB")
        n = safe_name(name)
        taken = {f["name"] for f in meta["files"]}
        stem, dot, ext = n.rpartition(".")
        i = 1
        while n in taken or n == "meta.json":
            n = f"{stem or ext} ({i}){dot}{ext if stem else ''}"
            i += 1
        (self.root / sid / n).write_bytes(data)
        meta["files"].append({"name": n, "type": mime[:100], "size": len(data)})
        self._save(sid, meta)
        return meta

    def file_path(self, sid: str, name: str) -> Path:
        meta = self.meta(sid)
        if name not in {f["name"] for f in meta["files"]}:
            raise ShareError("no such file in this share")
        return self._dir(sid) / name

    def mark_sent(self, sid: str, job_id: str) -> None:
        meta = self.meta(sid)
        meta["job_id"] = job_id
        self._save(sid, meta)

    def cleanup(self) -> int:
        if not self.root.is_dir():
            return 0
        n = 0
        for d in self.root.iterdir():
            m = d / "meta.json"
            try:
                old = self.clock() - json.loads(m.read_text(encoding="utf-8"))["created_at"] > self.keep
            except (OSError, ValueError, KeyError):
                old = self.clock() - d.stat().st_mtime > self.keep
            if d.is_dir() and old:
                shutil.rmtree(d, ignore_errors=True)
                n += 1
        return n
