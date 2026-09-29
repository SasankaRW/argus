"""Triggers: things outside Argus that start jobs.

**Folder triggers.** A worker watches a folder on its own machine (worker/watcher.py) and reports each file once
it has stopped changing. argusd remembers files by content (sha256) per plugin, so a copy, a re-download or a
restarted watcher never processes the same file twice ("merged"), and the plugin's queue limit holds back a flood:
the watcher keeps the rest and offers them again later ("limited").

**Webhooks.** POST /hooks/<name> with a signature made from the hook's secret (in .env, never in argus.yaml):
- style "argus": headers X-Argus-Timestamp (unix seconds, within 5 minutes) and
  X-Argus-Signature: sha256=HMAC(secret, "<timestamp>." + body); the timestamp stops replays.
- style "github": X-Hub-Signature-256: sha256=HMAC(secret, body), as GitHub sends it.
An optional delivery id (X-Argus-Id or X-GitHub-Delivery) merges retries of the same delivery.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import time
from collections.abc import Callable, Mapping
from typing import Any

from .config import Config, FolderTrigger, WebhookTrigger
from .db import Store
from .events import insert_event
from .jobs.store import JobStore

MAX_SKEW = 300  # seconds a signed webhook may be old or early
MAX_BODY = 256_000


class TriggerError(Exception):
    pass


class UnknownTrigger(TriggerError):
    pass


class BadSignature(TriggerError):
    pass


class Triggers:
    def __init__(self, store: Store, jobs: JobStore, cfg: Config, clock: Callable[[], float] = time.time):
        self.store = store
        self.jobs = jobs
        self.cfg = cfg
        self.clock = clock

    # -------------------------------------------------------------- folders

    def folders_for(self, worker_id: str, host: str) -> list[dict[str, Any]]:
        """The folder triggers a worker should watch (matched by worker id or host name)."""
        me = {worker_id.lower(), host.lower()}
        return [{"name": f.name, "path": f.path, "patterns": f.patterns, "ignore": f.ignore,
                 "settle_seconds": f.settle_seconds, "recursive": f.recursive}
                for f in self.cfg.triggers.folders if f.worker.lower() in me]

    def _folder(self, name: str) -> FolderTrigger:
        for f in self.cfg.triggers.folders:
            if f.name == name:
                return f
        raise UnknownTrigger(f"no folder trigger {name!r}")

    async def file(self, worker: str, name: str, path: str, sha256: str, size: int) -> dict[str, Any]:
        """A finished file in a watched folder. Returns {status: queued|duplicate, job_id}.
        Raises QueueFull when the plugin has too much waiting (the watcher offers it again later)."""
        f = self._folder(name)
        if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
            raise TriggerError("sha256 must be 64 lowercase hex characters")

        def fn(conn: sqlite3.Connection) -> dict[str, Any]:
            now = self.clock()
            row = conn.execute("SELECT job_id FROM files_seen WHERE plugin = ? AND sha256 = ?",
                               (f.plugin, sha256)).fetchone()
            if row is not None:
                insert_event(conn, now, "trigger.duplicate", job_id=row["job_id"], src=worker, dst=f.plugin,
                             data={"trigger": name, "path": path[-200:]})
                return {"status": "duplicate", "job_id": row["job_id"]}
            job_id, _ = self.jobs.enqueue_in(
                conn, now, f.plugin, f.workflow,
                {**f.input, "path": path, "sha256": sha256, "size": size, "trigger": name},
                needs=f.needs, priority=f.priority, model_group=f.model, window=f.window,
                dedupe_key=f"file:{f.plugin}:{sha256}", source=worker)
            conn.execute("INSERT INTO files_seen (plugin, sha256, path, trigger, job_id, created_at, updated_at)"
                         " VALUES (?,?,?,?,?,?,?)", (f.plugin, sha256, path, name, job_id, now, now))
            return {"status": "queued", "job_id": job_id}

        return await self.store.write(fn)  # QueueFull rolls the whole thing back, so the file stays "new"

    # -------------------------------------------------------------- webhooks

    def _hook(self, name: str) -> WebhookTrigger:
        for h in self.cfg.triggers.webhooks:
            if h.name == name:
                return h
        raise UnknownTrigger(f"no webhook {name!r}")

    def verify(self, name: str, headers: Mapping[str, str], body: bytes) -> WebhookTrigger:
        h = self._hook(name)
        secret = self.cfg.secrets.webhooks.get(name)
        if not secret:
            raise BadSignature("this webhook has no secret configured")
        lower = {k.lower(): v for k, v in headers.items()}
        if h.style == "github":
            given = lower.get("x-hub-signature-256", "")
            want = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        else:
            ts = lower.get("x-argus-timestamp", "")
            if not ts.isdigit() or abs(self.clock() - int(ts)) > MAX_SKEW:
                raise BadSignature("missing or stale X-Argus-Timestamp")
            given = lower.get("x-argus-signature", "")
            want = "sha256=" + hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(given, want):
            raise BadSignature("signature does not match")
        return h

    async def webhook(self, name: str, headers: Mapping[str, str], body: bytes) -> dict[str, Any]:
        if len(body) > MAX_BODY:
            raise TriggerError(f"body too large (limit {MAX_BODY} bytes)")
        h = self.verify(name, headers, body)
        lower = {k.lower(): v for k, v in headers.items()}
        delivery = (lower.get("x-argus-id") or lower.get("x-github-delivery") or "")[:100] or None
        try:
            payload: Any = json.loads(body) if body else None
        except ValueError:
            payload = body.decode("utf-8", errors="replace")
        extra = {"event": lower["x-github-event"]} if "x-github-event" in lower else {}

        def fn(conn: sqlite3.Connection) -> dict[str, Any]:
            now = self.clock()
            job_id, created = self.jobs.enqueue_in(
                conn, now, h.plugin, h.workflow, {**h.input, "hook": name, "body": payload, "delivery": delivery,
                                                   **extra},
                needs=h.needs, priority=h.priority, model_group=h.model, window=h.window,
                dedupe_key=f"hook:{name}:{delivery}" if delivery else None, source="webhooks")
            return {"status": "queued" if created else "duplicate", "job_id": job_id}

        return await self.store.write(fn)
