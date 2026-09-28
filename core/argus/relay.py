"""The ntfy reply relay: approval buttons that work from anywhere.

The phone's Approve / Reject buttons do not call Argus directly (the phone may not reach it). They post a short
line, "approve <approval id> <signed token>", to a private reply topic on the ntfy server. Argus keeps one
streaming subscription open to that topic (ntfy's /json stream: a tap arrives at once, and it stays well inside
ntfy.sh's request limits), decides the approval with the token (one-time, so a replayed or forged line does
nothing) and sends a short confirmation back. If the connection drops it reconnects, asking for everything
since the last message it handled.

The last message id read is kept in the database, so nothing is missed across a restart; ntfy.sh keeps messages
for 12 hours.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from .approvals import ApprovalClosed, ApprovalError, Approvals, BadToken
from .config import Config
from .db import Store
from .events import insert_event
from .outbox import Outbox, add_message, ntfy_message

log = logging.getLogger("argus.relay")

LINE = re.compile(r"^\s*(approve|reject)\s+([0-9A-Za-z]{10,40})\s+([0-9a-f]{16,64})\s*$")
SINCE_KEY = "ntfy_reply_since"


class ReplyRelay:
    def __init__(self, store: Store, cfg: Config, approvals: Approvals, outbox: Outbox,
                 clock: Callable[[], float] = time.time, fetch: Callable[[str], list[dict]] | None = None):
        self.store = store
        self.cfg = cfg
        self.approvals = approvals
        self.outbox = outbox
        self.clock = clock
        self._fetch = fetch or self._http_fetch
        self._injected = fetch is not None
        self._task: asyncio.Task | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop = threading.Event()
        self._resp: Any = None
        self.connected = False
        self.handled = 0
        self.last_error: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.approvals.reply_topic)

    # -------------------------------------------------------------- lifecycle

    def start(self) -> None:
        """Tests pass `fetch` and call poll_once(); the real relay streams from ntfy in a thread."""
        if not self.enabled or self._task is not None or self._thread is not None:
            return
        if self._injected:
            self._task = asyncio.create_task(self._poll_loop(), name="argus-ntfy-replies")
            return
        self._loop = asyncio.get_running_loop()
        self._stop.clear()
        self._thread = threading.Thread(target=self._stream_forever, name="argus-ntfy-replies", daemon=True)
        self._thread.start()

    async def stop(self) -> None:
        self._stop.set()
        resp = self._resp
        if resp is not None:
            with contextlib.suppress(Exception):
                resp.close()
        self._thread = None
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    @property
    def alive(self) -> bool:
        if not self.enabled:
            return True
        return bool((self._thread and self._thread.is_alive()) or (self._task and not self._task.done()))

    async def _poll_loop(self) -> None:
        while True:
            try:
                await self.poll_once()
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.last_error = str(e)
            await asyncio.sleep(self.cfg.ntfy.reply_retry_seconds)

    # -------------------------------------------------------------- streaming (runs in its own thread)

    def _run(self, coro, timeout: float = 30):
        assert self._loop is not None
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    def _stream_forever(self) -> None:
        delay = self.cfg.ntfy.reply_retry_seconds
        while not self._stop.is_set():
            try:
                since = self._run(self._since())
                url = f"{self.cfg.ntfy.url.rstrip('/')}/{self.approvals.reply_topic}/json?since={since}"
                req = urllib.request.Request(url, headers={"Accept": "application/x-ndjson"})
                if self.cfg.secrets.ntfy_token:
                    req.add_header("Authorization", f"Bearer {self.cfg.secrets.ntfy_token}")
                # ntfy sends a keepalive line every ~45 s, so a read that waits much longer means a dead link
                with urllib.request.urlopen(req, timeout=120) as resp:
                    self._resp = resp
                    self.connected = True
                    self.last_error = None
                    delay = self.cfg.ntfy.reply_retry_seconds
                    for raw in resp:
                        if self._stop.is_set():
                            return
                        try:
                            m = json.loads(raw)
                        except ValueError:
                            continue
                        if m.get("event") == "message":
                            self._run(self._handle_message(m))
            except RuntimeError:  # the event loop is gone: argusd is stopping
                return
            except Exception as e:
                if self._stop.is_set():
                    return
                if self.last_error != str(e):
                    log.warning("reply topic connection failed; retrying", extra={"error": str(e)})
                self.last_error = str(e)
                delay = min(max(delay * 2, 1.0), 60.0)
            finally:
                self._resp = None
                self.connected = False
            self._stop.wait(delay)

    async def _handle_message(self, m: dict) -> None:
        await self.handle(str(m.get("message", "")))
        await self._save_since(str(m["id"]))

    # -------------------------------------------------------------- polling (tests, and a manual check)

    def _http_fetch(self, since: str) -> list[dict]:
        url = f"{self.cfg.ntfy.url.rstrip('/')}/{self.approvals.reply_topic}/json?poll=1&since={since}"
        req = urllib.request.Request(url, headers={"Accept": "application/x-ndjson"})
        if self.cfg.secrets.ntfy_token:
            req.add_header("Authorization", f"Bearer {self.cfg.secrets.ntfy_token}")
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.ntfy.timeout_seconds) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"ntfy HTTP {e.code}") from None
        out = []
        for line in raw.splitlines():
            with contextlib.suppress(ValueError):
                m = json.loads(line)
                if m.get("event") == "message":
                    out.append(m)
        return out

    async def _since(self) -> str:
        def fn(conn: sqlite3.Connection) -> str | None:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (SINCE_KEY,)).fetchone()
            return row[0] if row else None

        # First run: look back 12 hours (ntfy.sh's cache); old or used lines are harmless (one-time tokens).
        return await self.store.read(fn) or str(int(self.clock() - 12 * 3600))

    async def _save_since(self, msg_id: str) -> None:
        def fn(conn: sqlite3.Connection) -> None:
            now = self.clock()
            conn.execute("INSERT INTO settings (key, value, created_at, updated_at) VALUES (?,?,?,?)"
                         " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                         (SINCE_KEY, msg_id, now, now))

        await self.store.write(fn)

    async def poll_once(self) -> int:
        """Read new lines from the reply topic and act on them. Returns how many were handled."""
        msgs = await asyncio.to_thread(self._fetch, await self._since())
        for m in msgs:
            await self.handle(str(m.get("message", "")))
        if msgs:
            await self._save_since(str(msgs[-1]["id"]))
        return len(msgs)

    # -------------------------------------------------------------- acting

    async def handle(self, line: str) -> str:
        """One button tap. Returns what happened: approved, rejected, closed, ignored."""
        m = LINE.match(line)
        if not m:
            return "ignored"
        answer, aid, token = m.groups()
        try:
            a = await self.approvals.decide(aid, answer, token=token, by="phone")
        except ApprovalClosed as e:
            # A second tap, or an old notification: say so, once per approval.
            await self._confirm(aid, f"already {e.state}", dedupe=f"closed:{aid}")
            return "closed"
        except (BadToken, ApprovalError) as e:
            log.warning("ignored a reply", extra={"approval": aid, "error": str(e)})
            await self._event("approval.reply_refused", aid, {"reason": str(e)})
            return "ignored"
        self.handled += 1
        await self._confirm(aid, a["state"], dedupe=f"confirm:{aid}", approval=a)
        return a["state"]

    async def _event(self, kind: str, aid: str, data: dict[str, Any]) -> None:
        await self.store.write(lambda c: insert_event(c, self.clock(), kind, src="ntfy", dst="approvals",
                                                      data={"approval": aid, **data}))

    async def _confirm(self, aid: str, state: str, *, dedupe: str, approval: dict | None = None) -> None:
        try:
            a = approval or await self.approvals.get(aid)
        except ApprovalError:
            return
        word = {"approved": "Approved", "rejected": "Rejected"}.get(state, state.capitalize())
        tags = {"approved": ["white_check_mark"], "rejected": ["x"]}.get(state, ["information_source"])
        msg = ntfy_message(f"{word}: {a['title']}", f"{a['plugin']} · decided on your phone"
                           if state in ("approved", "rejected") else f"{a['plugin']} · nothing changed",
                           priority="low", tags=tags)

        def fn(conn: sqlite3.Connection) -> None:
            now = self.clock()
            if add_message(conn, now, "ntfy", msg, dedupe_key=dedupe, job_id=a.get("job_id")):
                insert_event(conn, now, "approval.reply", job_id=a.get("job_id"), src="ntfy", dst="approvals",
                             data={"approval": aid, "result": state})

        await self.store.write(fn)
        self.outbox.poke()

    def health(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "alive": self.alive, "connected": self.connected, "handled": self.handled,
                "last_error": self.last_error}
