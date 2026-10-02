"""The outbox: messages that leave Argus (to the phone app, webhooks later), delivered exactly once.

A message is written to the `outbox` table in the same transaction as the change that caused it (an approval
created, a job dead), so it can never be lost: if argusd dies before sending, the row is still there and the
sender picks it up after the restart. A `dedupe_key` stops the same message being queued twice, for example
when a step is retried after a crash.

The phone app does not get messages pushed: it asks (a long poll, `GET /phone/inbox`), shows them as notifications
and acknowledges them (`POST /phone/inbox/ack`); only then is a row `sent`. Until then it waits in the table, so a
phone that is off, asleep or off Tailscale gets everything when it is back (messages older than `notify.keep_hours`
are dropped: an approval from two days ago is not worth a buzz). Other senders are push: the sender claims a row
(`sending`), sends it, and marks it `sent`; failures retry with backoff and after `notify.max_attempts` the row is
`failed` and shows in Helios.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
import time
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
                dedupe_key: str | None = None, job_id: str | None = None, send_at: float | None = None) -> str | None:
    """Queue a message. Call inside a Store write, in the same transaction as the change it reports.

    Returns the new row id, or None when a message with this dedupe_key was queued before."""
    if dedupe_key is not None and conn.execute(
            "SELECT 1 FROM outbox WHERE dedupe_key = ?", (dedupe_key,)).fetchone() is not None:
        return None
    oid = new_id()
    conn.execute(
        "INSERT INTO outbox (id, kind, dedupe_key, payload, state, next_try_at, job_id, created_at, updated_at)"
        " VALUES (?,?,?,?,'pending',?,?,?,?)",
        (oid, kind, dedupe_key, _dumps(payload), send_at or now, job_id, now, now),
    )
    if kind in ("phone", "ntfy") and _prio(payload.get("priority")) >= 4:  # important: Ari may say it aloud at the PC
        insert_event(conn, now, "ari.notice", job_id=job_id, src="argus", dst="ari",
                     data={"title": str(payload.get("title") or "")[:200],
                           "text": str(payload.get("message") or "")[:400]})
    return oid


def _prio(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return {"max": 5, "urgent": 5, "high": 4, "default": 3, "low": 2, "min": 1}.get(str(v), 3)


def phone_message(title: str, message: str, *, priority: str = "default", tags: list[str] | None = None,
                  click: str | None = None, actions: list[dict] | None = None) -> dict[str, Any]:
    """A message for the phone: title, text, priority (1 min .. 5 urgent), tags, a page to open on tap (`click`, a
    path on Argus or a full URL) and up to three buttons ({"action": "http" | "view", "label", "url", ...})."""
    msg: dict[str, Any] = {"title": title[:250], "message": message[:3500] or title,
                           "priority": PRIORITIES.index(priority) + 1 if priority in PRIORITIES else 3}
    if tags:
        msg["tags"] = tags
    if click:
        msg["click"] = click
    if actions:
        msg["actions"] = actions[:3]  # a notification shows at most three buttons
    return msg


class SendError(Exception):
    pass


class PhoneInbox:
    """The phone app's side of the outbox: it asks for its messages (nothing is pushed, nothing listens on the
    phone). Remembers when the app last asked, to show whether it is connected."""

    pulled = True  # the outbox leaves these rows for the app to collect and acknowledge
    enabled = True

    def __init__(self, clock: Callable[[], float] = time.time):
        self.clock = clock
        self.last_seen = 0.0
        self.device = ""
        self.waiters: list[asyncio.Event] = []

    def seen(self, device: str = "") -> None:
        self.last_seen = self.clock()
        if device:
            self.device = device[:60]

    def connected(self, within: float = 90.0) -> bool:
        return self.last_seen > 0 and self.clock() - self.last_seen < within

    def wake(self) -> None:
        for w in self.waiters:
            w.set()

    def send(self, payload: dict[str, Any]) -> None:  # never called: the app collects its messages
        raise SendError("the phone app collects its messages; nothing is pushed")


class Outbox:
    def __init__(self, store: Store, cfg: Config, clock: Callable[[], float] = time.time,
                 senders: dict[str, Any] | None = None, poll_seconds: float = 2.0):
        self.store = store
        self.cfg = cfg
        self.clock = clock
        self.senders = senders if senders is not None else {"phone": PhoneInbox(clock)}
        self.poll_seconds = poll_seconds
        self._wake: asyncio.Event | None = None
        self._task: asyncio.Task | None = None
        self.sent = 0
        self.last_error: str | None = None

    @property
    def inbox(self) -> PhoneInbox | None:
        """The phone app's inbox, when it is the one collecting messages."""
        box = self.senders.get("phone")
        return box if isinstance(box, PhoneInbox) else None

    def pulled_kinds(self) -> list[str]:
        """The kinds the app collects (the old name "ntfy" is the same thing: rows written before the move)."""
        out = [k for k, sd in self.senders.items() if getattr(sd, "pulled", False)]
        return out + ["ntfy"] if "phone" in out else out

    # -------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        def fn(conn: sqlite3.Connection) -> int:
            now = self.clock()
            if self.inbox is not None:
                ensure_component(conn, now, "phone-app", "service", "Phone app", "phone")
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
        if self.inbox is not None:
            self.inbox.wake()

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
            skip = self.pulled_kinds()
            rows = conn.execute("SELECT * FROM outbox WHERE state = 'pending' AND next_try_at <= ?"
                                f" AND kind NOT IN ({','.join('?' * len(skip))}) ORDER BY created_at LIMIT ?",
                                (now, *skip, limit)).fetchall()
            for r in rows:
                conn.execute("UPDATE outbox SET state = 'sending', attempts = attempts + 1, updated_at = ?"
                             " WHERE id = ?", (now, r["id"]))
            return [dict(r) for r in rows]

        await self.expire_inbox()
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
        max_attempts = self.cfg.notify.max_attempts

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

    # -------------------------------------------------------------- the phone app collects

    async def expire_inbox(self) -> int:
        """Messages the app never collected are dropped after notify.keep_hours."""
        skip = self.pulled_kinds()
        if not skip:
            return 0
        cutoff = self.clock() - self.cfg.notify.keep_hours * 3600

        def fn(conn: sqlite3.Connection) -> int:
            return conn.execute(
                f"UPDATE outbox SET state = 'skipped', last_error = 'not collected in time', updated_at = ?"
                f" WHERE state = 'pending' AND created_at < ? AND kind IN ({','.join('?' * len(skip))})",
                (self.clock(), cutoff, *skip)).rowcount

        return await self.store.write(fn)

    async def inbox_fetch(self, device: str = "", wait: float = 0.0, limit: int = 20) -> list[dict[str, Any]]:
        """What the app has not collected yet, oldest first; when there is nothing, waits up to `wait` seconds for
        something (a long poll). Asking again before acknowledging returns the same messages."""
        box = self.inbox
        if box is None:
            return []
        kinds = self.pulled_kinds()
        box.seen(device)

        def fn(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = conn.execute(
                f"SELECT id, payload, created_at FROM outbox WHERE state = 'pending' AND next_try_at <= ?"
                f" AND kind IN ({','.join('?' * len(kinds))}) ORDER BY created_at LIMIT ?",
                (self.clock(), *kinds, limit)).fetchall()
            return [{**json.loads(r["payload"]), "id": r["id"], "at": r["created_at"]} for r in rows]

        waiter = asyncio.Event()
        box.waiters.append(waiter)
        try:
            rows = await self.store.read(fn)
            if not rows and wait > 0:
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(wait):
                        await waiter.wait()
                box.seen(device)
                rows = await self.store.read(fn)
        finally:
            box.waiters.remove(waiter)
        return rows

    async def inbox_ack(self, ids: list[str]) -> int:
        """The app showed these: they are sent (the one-time approval tokens in the buttons are dropped from the
        stored copy)."""
        ids = [str(i) for i in ids][:100]
        if not ids:
            return 0

        def fn(conn: sqlite3.Connection) -> int:
            now, n = self.clock(), 0
            for oid in ids:
                r = conn.execute("SELECT * FROM outbox WHERE id = ? AND state = 'pending'", (oid,)).fetchone()
                if r is None:
                    continue
                payload = json.loads(r["payload"])
                payload.pop("actions", None)
                conn.execute("UPDATE outbox SET state = 'sent', sent_at = ?, payload = ?, last_error = NULL,"
                             " updated_at = ? WHERE id = ?", (now, _dumps(payload), now, oid))
                insert_event(conn, now, "outbox.sent", job_id=r["job_id"], src="argus", dst="phone-app",
                             data={"kind": r["kind"], "attempt": r["attempts"] + 1})
                n += 1
            return n

        n = await self.store.write(fn)
        self.sent += n
        return n

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

    async def notifications(self, limit: int = 50) -> list[dict[str, Any]]:
        """The phone notifications, newest first, with their text: what the app's Notifications page shows.
        `state`: waiting (the phone has not collected it yet), delivered, dropped (never collected in time)."""
        kinds = ["phone", "ntfy"]

        def fn(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = conn.execute(
                "SELECT id, state, payload, created_at, sent_at, dedupe_key FROM outbox WHERE kind IN (?,?)"
                " ORDER BY created_at DESC LIMIT ?", (*kinds, limit)).fetchall()
            out = []
            for r in rows:
                p = json.loads(r["payload"])
                key = r["dedupe_key"] or ""
                aid = key.split(":", 1)[1] if key.startswith(("approval:", "approval-remind:")) else None
                ap = conn.execute("SELECT state FROM approvals WHERE id = ?", (aid,)).fetchone() if aid else None
                out.append({"approval_id": aid if ap else None, "approval_state": ap["state"] if ap else None,
                            "id": r["id"], "title": p.get("title", ""), "text": p.get("message", ""),
                            "priority": _prio(p.get("priority")), "click": p.get("click"),
                            "at": r["created_at"], "delivered_at": r["sent_at"],
                            "state": {"sent": "delivered", "skipped": "dropped", "failed": "dropped"}.get(
                                r["state"], "waiting")})
            return out

        return await self.store.read(fn)

    def health(self) -> dict[str, Any]:
        box = self.inbox
        return {"alive": self.alive, "sent": self.sent, "last_error": self.last_error,
                "phone": bool(box and box.connected()), "phone_seen": box.last_seen if box else 0.0,
                "phone_device": box.device if box else ""}
