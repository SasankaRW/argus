"""Events: the record of everything Argus does, and the live stream Helios draws from.

Writing: every change calls `insert_event` inside the same transaction as the change itself, so an event
exists exactly when its change does. When an event names a sender and a receiver (`src` -> `dst`), the
`edges` table remembers that the two talked; the first time, an `edge.added` event is written too. That is
how the map grows by itself.

Streaming: each event has a `seq` (SQLite rowid). Rows are written by one thread and committed in order, so
`seq` only goes up in commit order. The `EventHub` tails the table from its last `seq` (woken right after
each commit, with a slow poll as a safety net) and hands batches to subscribers. A subscriber that falls
behind is dropped; it reconnects with `since=<last seq>` and replays the gap from the database, so nothing
is lost and one slow browser can never slow Argus down.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from .db import Store
from .ids import new_id

log = logging.getLogger("argus.events")

_COLS = "rowid AS seq, id, job_id, step, kind, from_component, to_component, data, at"


def _dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=str)


def insert_event(
    conn: sqlite3.Connection,
    now: float,
    kind: str,
    *,
    job_id: str | None = None,
    step: str | None = None,
    src: str | None = None,
    dst: str | None = None,
    data: dict[str, Any] | None = None,
) -> str:
    """Write one event (and update the src -> dst edge). Call inside a Store write."""
    event_id = new_id()
    conn.execute(
        "INSERT INTO events (id, job_id, step, kind, from_component, to_component, data, at, created_at,"
        " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (event_id, job_id, step, kind, src, dst, _dumps(data) if data else None, now, now, now),
    )
    if src and dst and src != dst:
        cur = conn.execute(
            "UPDATE edges SET count = count + 1, last_kind = ?, last_seen = ? WHERE src = ? AND dst = ?",
            (kind, now, src, dst),
        )
        if cur.rowcount == 0:
            conn.execute(
                "INSERT INTO edges (src, dst, count, last_kind, first_seen, last_seen) VALUES (?,?,1,?,?,?)",
                (src, dst, kind, now, now),
            )
            conn.execute(
                "INSERT INTO events (id, kind, from_component, to_component, data, at, created_at, updated_at)"
                " VALUES (?, 'edge.added', ?, ?, ?, ?, ?, ?)",
                (new_id(), src, dst, _dumps({"first_kind": kind}), now, now, now),
            )
    return event_id


def event_row(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "seq": r["seq"],
        "id": r["id"],
        "kind": r["kind"],
        "job_id": r["job_id"],
        "step": r["step"],
        "from": r["from_component"],
        "to": r["to_component"],
        "data": json.loads(r["data"]) if r["data"] else None,
        "at": r["at"],
    }


@dataclass(frozen=True)
class EventFilter:
    """Which events a reader wants. `kinds` are prefixes: "job." matches job.queued, job.leased, ..."""

    kinds: tuple[str, ...] = ()
    job_id: str | None = None

    @classmethod
    def parse(cls, kinds: str | Iterable[str] | None = None, job_id: str | None = None) -> EventFilter:
        if isinstance(kinds, str):
            kinds = [k for k in (x.strip() for x in kinds.split(",")) if k]
        return cls(tuple(kinds or ()), job_id or None)

    def matches(self, ev: dict[str, Any]) -> bool:
        if self.job_id and ev["job_id"] != self.job_id:
            return False
        return not self.kinds or any(ev["kind"].startswith(k) for k in self.kinds)

    def sql(self) -> tuple[str, list[Any]]:
        parts, args = [], []
        if self.job_id:
            parts.append("job_id = ?")
            args.append(self.job_id)
        if self.kinds:
            parts.append("(" + " OR ".join("kind LIKE ? ESCAPE '\\'" for _ in self.kinds) + ")")
            args += [k.replace("%", "").replace("_", r"\_") + "%" for k in self.kinds]
        return (" AND " + " AND ".join(parts)) if parts else "", args


def read_events(conn: sqlite3.Connection, after: int, until: int | None = None, limit: int = 500,
                flt: EventFilter | None = None) -> list[dict[str, Any]]:
    where, args = (flt or EventFilter()).sql()
    if until is not None:
        where += " AND rowid <= ?"
        args.append(until)
    sql = f"SELECT {_COLS} FROM events WHERE rowid > ?{where} ORDER BY rowid LIMIT ?"
    return [event_row(r) for r in conn.execute(sql, (after, *args, limit)).fetchall()]


def last_seq(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM events").fetchone()[0]


# ---------------------------------------------------------------------- live stream


@dataclass(eq=False)
class Subscriber:
    flt: EventFilter
    queue: asyncio.Queue
    after: int  # the hub's cursor when this subscriber joined: live batches start after it
    dropped: str | None = None  # why the hub let go: "slow" or "shutdown"
    delivered: int = 0
    joined: float = field(default_factory=time.time)


class EventHub:
    def __init__(self, store: Store, *, batch_window: float = 0.05, poll_interval: float = 1.0,
                 queue_size: int = 200, read_limit: int = 1000):
        self.store = store
        self.batch_window = batch_window
        self.poll_interval = poll_interval
        self.queue_size = queue_size
        self.read_limit = read_limit
        self.cursor = 0
        self.subscribers: set[Subscriber] = set()
        self.published = 0
        self.dropped = 0
        self.last_error: str | None = None
        self._wake: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._listener: Callable[[], None] | None = None

    # -------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        self.cursor = await self.store.read(last_seq)

        def on_commit() -> None:  # runs on the writer thread
            loop, wake = self._loop, self._wake
            if loop is not None and wake is not None and not loop.is_closed():
                loop.call_soon_threadsafe(wake.set)

        self._listener = on_commit
        self.store.on_commit(on_commit)
        self._task = asyncio.create_task(self._run(), name="argus-event-hub")

    async def stop(self) -> None:
        if self._listener is not None:
            self.store.remove_commit_listener(self._listener)
            self._listener = None
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        for sub in list(self.subscribers):
            self._drop(sub, "shutdown")

    @property
    def alive(self) -> bool:
        return self._task is not None and not self._task.done()

    # -------------------------------------------------------------- subscribers

    def subscribe(self, flt: EventFilter | None = None) -> Subscriber:
        sub = Subscriber(flt or EventFilter(), asyncio.Queue(self.queue_size), self.cursor)
        self.subscribers.add(sub)
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        self.subscribers.discard(sub)

    def _drop(self, sub: Subscriber, reason: str) -> None:
        sub.dropped = reason
        self.subscribers.discard(sub)
        try:
            sub.queue.put_nowait(None)  # tells the reader to stop
        except asyncio.QueueFull:
            try:
                sub.queue.get_nowait()
                sub.queue.put_nowait(None)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass

    # -------------------------------------------------------------- tailing

    async def _run(self) -> None:
        assert self._wake is not None
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.poll_interval)
            except TimeoutError:
                pass
            self._wake.clear()
            if self.batch_window:
                await asyncio.sleep(self.batch_window)  # gather a burst into one batch
            try:
                await self.drain()
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as e:  # keep tailing; the next wake or poll retries
                self.last_error = str(e)
                log.exception("event hub read failed")
                await asyncio.sleep(0.5)

    async def drain(self) -> int:
        """Read every event after the cursor and hand it out. Returns how many were read."""
        total = 0
        while True:
            after = self.cursor
            rows = await self.store.read(lambda c, a=after: read_events(c, a, limit=self.read_limit))
            if not rows:
                return total
            # No awaits from here to the end of the loop body: cursor and fan-out move together.
            self.cursor = rows[-1]["seq"]
            self.published += len(rows)
            total += len(rows)
            for sub in list(self.subscribers):
                mine = [e for e in rows if e["seq"] > sub.after and sub.flt.matches(e)]
                if not mine:
                    continue
                try:
                    sub.queue.put_nowait(mine)
                    sub.delivered += len(mine)
                except asyncio.QueueFull:
                    self.dropped += 1
                    log.warning("event subscriber too slow, dropped", extra={"behind": sub.queue.qsize()})
                    self._drop(sub, "slow")
            if len(rows) < self.read_limit:
                return total

    def stats(self) -> dict[str, Any]:
        return {
            "alive": self.alive,
            "seq": self.cursor,
            "subscribers": len(self.subscribers),
            "published": self.published,
            "dropped_subscribers": self.dropped,
            "last_error": self.last_error,
        }


# ---------------------------------------------------------------------- retention


def prune_events(conn: sqlite3.Connection, older_than: float, batch: int = 5000) -> int:
    """Delete up to `batch` events older than `older_than` (epoch seconds). Never deletes the newest event,
    so `seq` keeps counting up."""
    newest = last_seq(conn)
    cur = conn.execute(
        "DELETE FROM events WHERE rowid IN (SELECT rowid FROM events WHERE at < ? AND rowid < ?"
        " ORDER BY rowid LIMIT ?)",
        (older_than, newest, batch),
    )
    return cur.rowcount
