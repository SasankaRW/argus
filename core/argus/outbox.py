"""The outbox: messages that leave Argus (ntfy now, webhooks later), sent exactly once.

A message is written to the `outbox` table in the same transaction as the change that caused it (an approval
created, a job dead), so it can never be lost: if argusd dies before sending, the row is still there and the
sender picks it up after the restart. A `dedupe_key` stops the same message being queued twice, for example
when a step is retried after a crash.

The sender claims a row (`sending`), sends it, and marks it `sent`. A crash between the phone receiving it
and the row being marked (milliseconds) is the only case that can repeat a message. Failures retry with
backoff; after `ntfy.max_attempts` the row is `failed` and shows in Helios.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from .config import Config
from .db import Store
from .events import insert_event
from .ids import new_id
from .registry import ensure_component

log = logging.getLogger("argus.outbox")

BACKOFF = (5, 30, 120, 600, 1800, 3600)
PRIORITIES = ("min", "low", "default", "high", "urgent")


def _dumps(v: Any) -> str:
    return json.dumps(v, separators=(",", ":"), ensure_ascii=False, default=str)


def add_message(conn: sqlite3.Connection, now: float, kind: str, payload: dict[str, Any], *,
                dedupe_key: str | None = None, job_id: str | None = None) -> str | None:
    """Queue a message. Call inside a Store write, in the same transaction as the change it reports.

    Returns the new row id, or None when a message with this dedupe_key was queued before."""
    if dedupe_key is not None and conn.execute(
            "SELECT 1 FROM outbox WHERE dedupe_key = ?", (dedupe_key,)).fetchone() is not None:
        return None
    oid = new_id()
    conn.execute(
        "INSERT INTO outbox (id, kind, dedupe_key, payload, state, next_try_at, job_id, created_at, updated_at)"
        " VALUES (?,?,?,?,'pending',?,?,?,?)",
        (oid, kind, dedupe_key, _dumps(payload), now, job_id, now, now),
    )
    return oid


def ntfy_message(title: str, message: str, *, priority: str = "default", tags: list[str] | None = None,
                 click: str | None = None, actions: list[dict] | None = None) -> dict[str, Any]:
    """An ntfy JSON message (without the topic; the sender adds it)."""
    msg: dict[str, Any] = {"title": title[:250], "message": message[:3500] or title,
                           "priority": PRIORITIES.index(priority) + 1 if priority in PRIORITIES else 3}
    if tags:
        msg["tags"] = tags
    if click:
        msg["click"] = click
    if actions:
        msg["actions"] = actions[:3]  # ntfy shows at most three buttons
    return msg


class SendError(Exception):
    pass


class NtfySender:
    """Posts one message to ntfy. Standard library only; runs in a thread."""

    def __init__(self, url: str, topic: str | None, token: str | None = None, timeout: float = 10):
        self.url = url.rstrip("/")
        self.topic = topic
        self.token = token
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.topic)

    def send(self, payload: dict[str, Any]) -> None:
        body = {**payload, "topic": self.topic}
        req = urllib.request.Request(self.url, data=_dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp.read()
        except urllib.error.HTTPError as e:
            raise SendError(f"ntfy HTTP {e.code}: {e.read().decode(errors='replace')[:200]}") from None
        except (urllib.error.URLError, OSError) as e:
            raise SendError(f"ntfy not reachable: {e}") from None


class Outbox:
    def __init__(self, store: Store, cfg: Config, clock: Callable[[], float] = time.time,
                 senders: dict[str, Any] | None = None, poll_seconds: float = 2.0):
        self.store = store
        self.cfg = cfg
        self.clock = clock
        self.senders = senders if senders is not None else {
            "ntfy": NtfySender(cfg.ntfy.url, cfg.secrets.ntfy_topic, cfg.secrets.ntfy_token,
                               cfg.ntfy.timeout_seconds)}
        self.poll_seconds = poll_seconds
        self._wake: asyncio.Event | None = None
        self._task: asyncio.Task | None = None
        self.sent = 0
        self.last_error: str | None = None

    # -------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        def fn(conn: sqlite3.Connection) -> int:
            now = self.clock()
            if self.senders.get("ntfy") is not None and self.senders["ntfy"].enabled:
                ensure_component(conn, now, "ntfy", "service", "ntfy", "cloud")
            # A crash while sending: the message may or may not have left. Send it again rather than lose it.
            return conn.execute("UPDATE outbox SET state = 'pending', updated_at = ? WHERE state = 'sending'",
                                (now,)).rowcount

        n = await self.store.write(fn)
        if n:
            log.warning("resending messages interrupted by a restart", extra={"count": n})
        self._wake = asyncio.Event()
        self._task = asyncio.create_task(self._loop(), name="argus-outbox")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    @property
    def alive(self) -> bool:
        return self._task is not None and not self._task.done()

    def poke(self) -> None:
        """Something was queued: send now instead of at the next poll."""
        if self._wake is not None:
            self._wake.set()

    async def _loop(self) -> None:
        assert self._wake is not None
        while True:
            try:
                while await self.send_due():
                    pass
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as e:  # keep going
                self.last_error = str(e)
                log.exception("outbox loop failed")
            with contextlib.suppress(TimeoutError):  # asyncio.timeout: see events.EventHub._run
                async with asyncio.timeout(self.poll_seconds):
                    await self._wake.wait()
            self._wake.clear()

    # -------------------------------------------------------------- sending

    async def send_due(self, limit: int = 20) -> int:
        """Send what is due. Returns how many rows were handled."""

        def claim(conn: sqlite3.Connection) -> list[dict]:
            now = self.clock()
            rows = conn.execute("SELECT * FROM outbox WHERE state = 'pending' AND next_try_at <= ?"
                                " ORDER BY created_at LIMIT ?", (now, limit)).fetchall()
            for r in rows:
                conn.execute("UPDATE outbox SET state = 'sending', attempts = attempts + 1, updated_at = ?"
                             " WHERE id = ?", (now, r["id"]))
            return [dict(r) for r in rows]

        rows = await self.store.write(claim)
        for r in rows:
            sender = self.senders.get(r["kind"])
            if sender is None or not getattr(sender, "enabled", True):
                await self._finish(r, "skipped", None)
                continue
            try:
                await asyncio.to_thread(sender.send, json.loads(r["payload"]))
            except Exception as e:
                await self._finish(r, "retry", str(e)[:500])
                continue
            await self._finish(r, "sent", None)
        return len(rows)

    async def _finish(self, r: dict, outcome: str, error: str | None) -> None:
        max_attempts = self.cfg.ntfy.max_attempts

        def fn(conn: sqlite3.Connection) -> None:
            now = self.clock()
            if outcome == "sent":
                # The one-time approval tokens are in the button URLs; once delivered, drop them from the copy.
                payload = json.loads(r["payload"])
                payload.pop("actions", None)
                conn.execute("UPDATE outbox SET state = 'sent', sent_at = ?, payload = ?, last_error = NULL,"
                             " updated_at = ? WHERE id = ?", (now, _dumps(payload), now, r["id"]))
                insert_event(conn, now, "outbox.sent", job_id=r["job_id"], src="argus", dst=r["kind"],
                             data={"kind": r["kind"], "attempt": r["attempts"] + 1})
            elif outcome == "skipped":
                conn.execute("UPDATE outbox SET state = 'skipped', updated_at = ? WHERE id = ?", (now, r["id"]))
            else:
                attempt = r["attempts"] + 1
                if attempt >= max_attempts:
                    conn.execute("UPDATE outbox SET state = 'failed', last_error = ?, updated_at = ? WHERE id = ?",
                                 (error, now, r["id"]))
                    insert_event(conn, now, "outbox.failed", job_id=r["job_id"], src="argus", dst=r["kind"],
                                 data={"error": error, "attempt": attempt})
                else:
                    delay = BACKOFF[min(attempt - 1, len(BACKOFF) - 1)]
                    conn.execute("UPDATE outbox SET state = 'pending', last_error = ?, next_try_at = ?,"
                                 " updated_at = ? WHERE id = ?", (error, now + delay, now, r["id"]))
                    insert_event(conn, now, "outbox.retry", job_id=r["job_id"], src="argus", dst=r["kind"],
                                 data={"error": error, "attempt": attempt, "retry_in": delay})

        await self.store.write(fn)
        if outcome == "sent":
            self.sent += 1
        elif outcome == "retry":
            self.last_error = error
            log.warning("message not sent, will retry", extra={"kind": r["kind"], "error": error})

    # -------------------------------------------------------------- reads

    async def stats(self) -> dict[str, Any]:
        def fn(conn: sqlite3.Connection) -> dict[str, Any]:
            counts = {r[0]: r[1] for r in conn.execute("SELECT state, COUNT(*) FROM outbox GROUP BY state")}
            oldest = conn.execute("SELECT MIN(created_at) FROM outbox WHERE state IN ('pending','sending')"
                                  ).fetchone()[0]
            return {"counts": counts, "oldest_pending_seconds": round(self.clock() - oldest, 1) if oldest else 0}

        return await self.store.read(fn)

    async def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        def fn(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = conn.execute("SELECT id, kind, state, attempts, last_error, job_id, payload, created_at,"
                                " sent_at FROM outbox ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                p = json.loads(d.pop("payload"))
                d["title"] = p.get("title")
                out.append(d)
            return out

        return await self.store.read(fn)

    def health(self) -> dict[str, Any]:
        ntfy = self.senders.get("ntfy")
        return {"alive": self.alive, "sent": self.sent, "last_error": self.last_error,
                "ntfy": bool(ntfy and getattr(ntfy, "enabled", False))}
