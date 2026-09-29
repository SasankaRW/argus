"""Folder watcher: runs inside a worker, on the machine that has the folder.

Every few seconds it looks at the folder. A file counts as finished once its size and modified time have not
changed for `settle_seconds` (a browser still downloading, or a copy in progress, keeps changing). Then it is
hashed and reported to argusd, which starts the plugin's job, or answers "duplicate" if that content was handled
before. When the plugin's queue is full (HTTP 429) the watcher backs off and offers the rest later, so a flood of
500 files never floods the queue. Temporary download files (*.crdownload, *.part, ...) are ignored.

Polling instead of OS file events: it works the same on Windows and Linux, on network drives too, and a missed
event can never lose a file.
"""

from __future__ import annotations

import fnmatch
import hashlib
import logging
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .client import ApiError, ArgusClient, Unreachable

log = logging.getLogger("argus.watcher")

BACKOFF = 30.0  # seconds to wait after "queue full"
PER_SCAN = 50  # files reported per scan at most, so one scan never takes long


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class FolderWatcher(threading.Thread):
    def __init__(self, client: ArgusClient, worker_id: str, trigger: dict[str, Any], *, poll: float = 5.0,
                 clock: Callable[[], float] = time.monotonic):
        super().__init__(name=f"watch-{trigger['name']}", daemon=True)
        self.client = client
        self.worker_id = worker_id
        self.t = trigger
        self.root = Path(trigger["path"])
        self.poll = poll
        self.clock = clock
        self._stop = threading.Event()
        self._seen: dict[str, tuple[tuple[int, int], float]] = {}  # path -> (size, mtime_ns), since when
        self._done: dict[str, tuple[int, int]] = {}  # path -> the (size, mtime_ns) already reported
        self.backoff_until = 0.0
        self.reported = 0
        self.duplicates = 0
        self.last_error: str | None = None

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        log.info("watching folder", extra={"trigger": self.t["name"], "path": str(self.root)})
        while not self._stop.is_set():
            try:
                self.scan_once()
            except Exception as e:  # a bad scan must not end the watcher
                self.last_error = str(e)
                log.exception("folder scan failed", extra={"trigger": self.t["name"]})
            self._stop.wait(self.poll)

    # -------------------------------------------------------------- one pass

    def _wanted(self, name: str) -> bool:
        if any(fnmatch.fnmatch(name, p) for p in self.t.get("ignore", [])):
            return False
        return any(fnmatch.fnmatch(name, p) for p in self.t.get("patterns", ["*"]))

    def _files(self) -> list[Path]:
        if not self.root.is_dir():
            self.last_error = f"folder not found: {self.root}"
            return []
        it = self.root.rglob("*") if self.t.get("recursive") else self.root.iterdir()
        return sorted(p for p in it if p.is_file() and self._wanted(p.name))

    def scan_once(self) -> dict[str, int]:
        """Look once. Returns counts: reported, duplicate, waiting (still settling or held back)."""
        now = self.clock()
        out = {"reported": 0, "duplicate": 0, "waiting": 0}
        present = set()
        for p in self._files():
            key = str(p)
            present.add(key)
            try:
                st = p.stat()
            except OSError:
                continue
            sig = (st.st_size, st.st_mtime_ns)
            if self._done.get(key) == sig:
                continue
            prev = self._seen.get(key)
            if prev is None or prev[0] != sig:
                self._seen[key] = (sig, now)  # new or still changing: start the settle clock
                if self.t.get("settle_seconds", 120) > 0:
                    out["waiting"] += 1
                    continue
                prev = self._seen[key]
            if now - prev[1] < self.t.get("settle_seconds", 120) or now < self.backoff_until \
                    or out["reported"] + out["duplicate"] >= PER_SCAN:
                out["waiting"] += 1
                continue
            try:
                digest = sha256_file(p)  # a file still locked by the browser fails here: try next time
            except OSError:
                out["waiting"] += 1
                continue
            try:
                r = self.client.post("/triggers/file", {"worker": self.worker_id, "trigger": self.t["name"],
                                                        "path": key, "sha256": digest, "size": st.st_size},
                                     retry=False)
            except ApiError as e:
                if e.status == 429:  # the plugin has enough waiting: hold the rest back for a while
                    self.backoff_until = now + BACKOFF
                    out["waiting"] += 1
                    continue
                self.last_error = str(e)
                log.warning("file not accepted", extra={"path": key, "error": str(e)})
                self._done[key] = sig  # don't hammer argusd with a file it refuses; a change retries it
                continue
            except Unreachable as e:
                self.last_error = str(e)
                self.backoff_until = now + BACKOFF
                out["waiting"] += 1
                continue
            self._done[key] = sig
            self._seen.pop(key, None)
            if r.get("status") == "duplicate":
                out["duplicate"] += 1
                self.duplicates += 1
            else:
                out["reported"] += 1
                self.reported += 1
        for gone in set(self._seen) - present:  # deleted or moved away
            self._seen.pop(gone, None)
        for gone in set(self._done) - present:
            self._done.pop(gone, None)
        return out


def start_watchers(client: ArgusClient, worker_id: str, folders: list[dict[str, Any]]) -> list[FolderWatcher]:
    watchers = []
    for t in folders:
        if not os.path.isdir(t["path"]):
            log.warning("watched folder does not exist (yet); will keep checking",
                        extra={"trigger": t["name"], "path": t["path"]})
        w = FolderWatcher(client, worker_id, t)
        w.start()
        watchers.append(w)
    return watchers
