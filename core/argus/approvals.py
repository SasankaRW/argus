"""Approvals: a job asks you, waits without holding a worker, and carries on with your answer.

    ctx.approve(...) in a step  ->  POST /jobs/{id}/approvals  ->  approval row + ntfy message (one transaction)
    the worker parks the job (waiting) and moves on to other work
    you tap Approve on the phone (or in Helios)  ->  decide()  ->  job back in the queue, ahead of scheduled work
    the step runs again, ctx.approve(...) now returns your answer at once

Rules:
- **Asking twice is safe.** Each approval has a key (`<job>:<step>:<n>`); a retried step gets the same approval
  back instead of a second one, and the notification is queued only once.
- **One-time signed tokens.** The phone buttons carry `HMAC(install key, approval id)`. It works only while the
  approval is pending, so a used or old link does nothing. Helios and apps use the normal Argus token instead.
- **Buttons.** By default they go straight to Argus over Tailscale (`approvals.public_url`), so knowing the ntfy
  topic is not enough to approve anything; when the phone comes back online Argus pushes what is still waiting
  (presence.py). With `approvals.buttons: ntfy` they post to a private reply topic instead (relay.py).
- **Money is shown from code.** A batch's count and total are added up here (Decimal), never taken from a model.
- **Waiting is not failing.** After `remind_hours` you get one reminder; after `expire_hours` the approval
  counts as "no" and the job carries on.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Any

from .config import Config
from .db import Store
from .events import insert_event
from .ids import new_id
from .jobs.states import JobState
from .jobs.store import JobStore
from .outbox import add_message, ntfy_message
from .registry import ensure_component

TYPES = ("entry", "batch", "draft")
RESUME_PRIORITY = 80  # jobs resuming after an approval go before scheduled work
MAX_PAYLOAD = 64_000


class ApprovalError(Exception):
    pass


class ApprovalNotFound(ApprovalError):
    pass


class ApprovalClosed(ApprovalError):
    """Already decided or expired."""

    def __init__(self, state: str):
        super().__init__(f"this approval is already {state}")
        self.state = state


class BadToken(ApprovalError):
    pass


def _dumps(v: Any) -> str:
    return json.dumps(v, separators=(",", ":"), ensure_ascii=False, default=str)


def money(v: Any) -> Decimal | None:
    try:
        d = Decimal(str(v).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def fmt_money(d: Decimal) -> str:
    return f"{d.quantize(Decimal('0.01')):,}"


def summarize(type_: str, fields: dict, items: list[dict]) -> dict[str, Any]:
    """Numbers shown on the card, computed in code."""
    out: dict[str, Any] = {}
    if type_ == "batch":
        out["count"] = len(items)
        amounts = [money(i.get("amount")) for i in items if isinstance(i, dict) and "amount" in i]
        if amounts and all(a is not None for a in amounts):
            out["total"] = fmt_money(sum(amounts, Decimal(0)))  # type: ignore[arg-type]
    elif "amount" in fields and money(fields["amount"]) is not None:
        out["amount"] = fmt_money(money(fields["amount"]))  # type: ignore[arg-type]
    return out


def _decode(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    d["payload"] = json.loads(d["payload"])
    d["answer"] = json.loads(d["answer"]) if d["answer"] is not None else None
    d.pop("token_hash", None)
    return d


class Approvals:
    def __init__(self, store: Store, jobs: JobStore, cfg: Config, clock: Callable[[], float] = time.time):
        self.store = store
        self.jobs = jobs
        self.cfg = cfg
        self.clock = clock
        self._key: bytes | None = None

    # -------------------------------------------------------------- tokens

    async def start(self) -> None:
        """Load (or create once) the install key the phone tokens are signed with."""

        def fn(conn: sqlite3.Connection) -> str:
            row = conn.execute("SELECT value FROM settings WHERE key = 'approval_key'").fetchone()
            if row is not None:
                return row[0]
            key = secrets.token_hex(32)
            now = self.clock()
            conn.execute("INSERT INTO settings (key, value, created_at, updated_at) VALUES ('approval_key',?,?,?)",
                         (key, now, now))
            return key

        self._key = bytes.fromhex(await self.store.write(fn))

    def token(self, approval_id: str) -> str:
        if self._key is None:
            raise RuntimeError("Approvals.start() has not run")
        return hmac.new(self._key, approval_id.encode(), hashlib.sha256).hexdigest()[:40]

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    @property
    def reply_topic(self) -> str | None:
        """The private ntfy topic the phone buttons post to and Argus listens on. NTFY_REPLY_TOPIC, or derived
        from the main topic and the install key, so there is nothing extra to set up."""
        sec = self.cfg.secrets
        if self.cfg.approvals.buttons != "ntfy":
            return None
        if sec.ntfy_reply_topic:
            return sec.ntfy_reply_topic
        if not sec.ntfy_topic or self._key is None:
            return None
        return f"{sec.ntfy_topic}-reply-{hmac.new(self._key, b'reply', hashlib.sha256).hexdigest()[:10]}"

    def reply_body(self, approval_id: str, answer: str) -> str:
        return f"{answer} {approval_id} {self.token(approval_id)}"

    def links(self, approval_id: str) -> dict[str, str] | None:
        """Direct links to Argus (need the phone to reach it, e.g. over Tailscale)."""
        base = self.cfg.approvals.public_url
        if not base:
            return None
        t = self.token(approval_id)
        return {"page": f"{base}/a/{approval_id}?t={t}",
                "approve": f"{base}/approvals/{approval_id}/decide?t={t}&answer=approve",
                "reject": f"{base}/approvals/{approval_id}/decide?t={t}&answer=reject"}

    # -------------------------------------------------------------- the notification

    def _message(self, a: dict[str, Any], *, reminder: bool = False) -> dict[str, Any]:
        p = a["payload"]
        lines: list[str] = []
        if a["type"] == "batch":
            s = f"{p.get('count', 0)} items"
            if p.get("total"):
                s += f" · total {p['total']}"
            lines.append(s)
            for it in (p.get("items") or [])[:5]:
                name = it.get("label") or it.get("name") or it.get("title") or ""
                amt = money(it.get("amount")) if "amount" in it else None
                lines.append(f"• {name}" + (f"  {fmt_money(amt)}" if amt is not None else ""))
        elif a["type"] == "draft":
            lines += [str(x) for x in (p.get("summary") or [])[:6]]
        else:
            for k, v in list((p.get("fields") or {}).items())[:10]:
                shown = p["amount"] if k == "amount" and p.get("amount") else v
                lines.append(f"{k}: {shown}")
        links = self.links(a["id"])
        reply = self.reply_topic
        ok_label = "Approve all" if a["type"] == "batch" else "Approve"
        actions: list[dict] = []
        if reply:
            # The buttons post to the reply topic on the ntfy server, which the phone can always reach; Argus reads
            # it from there. So they work with or without Tailscale.
            url = f"{self.cfg.ntfy.url.rstrip('/')}/{reply}"
            actions += [
                {"action": "http", "label": ok_label, "url": url, "method": "POST",
                 "body": self.reply_body(a["id"], "approve"), "clear": True},
                {"action": "http", "label": "Reject", "url": url, "method": "POST",
                 "body": self.reply_body(a["id"], "reject"), "clear": True},
            ]
        elif links:
            actions += [
                {"action": "http", "label": ok_label, "url": links["approve"], "method": "POST", "clear": True},
                {"action": "http", "label": "Reject", "url": links["reject"], "method": "POST", "clear": True},
            ]
        if links:
            actions.append({"action": "view", "label": "Open", "url": links["page"]})
            if not reply:
                lines.append("Approve / Reject need Tailscale.")
        if not actions:
            lines.append("Decide in Helios.")
        title = f"{'Reminder: ' if reminder else ''}{a['plugin']}: {a['title']}"
        return ntfy_message(title, "\n".join(lines), priority="high", tags=["inbox_tray"],
                            click=links["page"] if links else None, actions=actions or None)

    # -------------------------------------------------------------- worker side

    async def request(self, job_id: str, worker: str, key: str, type_: str, title: str, *,
                      fields: dict | None = None, items: list | None = None, summary: list | None = None,
                      link: str | None = None, step: str | None = None) -> tuple[dict[str, Any], bool]:
        """Create the approval for `key`, or return the one that already exists. Returns (approval, created)."""
        if type_ not in TYPES:
            raise ApprovalError(f"type must be one of {', '.join(TYPES)}")
        fields, items, summary = fields or {}, items or [], summary or []
        payload = {"fields": fields, "items": items, "summary": summary, "link": link,
                   **summarize(type_, fields, items)}
        raw = _dumps(payload)
        if len(raw) > MAX_PAYLOAD:
            raise ApprovalError(f"approval too large ({len(raw)} bytes, limit {MAX_PAYLOAD}); link to the full list")
        cfg = self.cfg.approvals

        def fn(conn: sqlite3.Connection) -> tuple[dict[str, Any], bool]:
            now = self.clock()
            job = self.jobs._get(conn, job_id)
            self.jobs._check_lease(job, worker, now)
            row = conn.execute("SELECT * FROM approvals WHERE key = ?", (key,)).fetchone()
            if row is not None:
                return _decode(row), False
            aid = new_id()
            conn.execute(
                "INSERT INTO approvals (id, job_id, key, plugin, step, type, title, payload, state, token_hash,"
                " expires_at, remind_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?, 'pending',?,?,?,?,?)",
                (aid, job_id, key, job.plugin, step, type_, title[:200], raw, self._hash(self.token(aid)),
                 now + cfg.expire_hours * 3600, now + cfg.remind_hours * 3600, now, now),
            )
            ensure_component(conn, now, "approvals", "service", "Approvals", None)
            a = _decode(conn.execute("SELECT * FROM approvals WHERE id = ?", (aid,)).fetchone())
            insert_event(conn, now, "approval.requested", job_id=job_id, step=step, src=job.plugin,
                         dst="approvals", data={"approval": aid, "type": type_, "title": a["title"]})
            add_message(conn, now, "ntfy", self._message(a), dedupe_key=f"approval:{aid}", job_id=job_id)
            return a, True

        return await self.store.write(fn)

    # -------------------------------------------------------------- deciding

    async def decide(self, approval_id: str, answer: str, *, fields: dict | None = None, by: str = "helios",
                     token: str | None = None) -> dict[str, Any]:
        """Approve or reject. With `token` (phone buttons) the signed one-time token must match."""
        if answer not in ("approve", "reject"):
            raise ApprovalError("answer must be approve or reject")

        def fn(conn: sqlite3.Connection) -> dict[str, Any]:
            now = self.clock()
            row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
            if row is None:
                raise ApprovalNotFound(approval_id)
            if token is not None and not (
                    row["token_hash"] and hmac.compare_digest(self._hash(token), row["token_hash"])
                    and hmac.compare_digest(token, self.token(approval_id))):
                # a used token has no hash left, so it ends up here too
                if row["state"] != "pending":
                    raise ApprovalClosed(row["state"])
                raise BadToken("this link is not valid")
            if row["state"] != "pending":
                raise ApprovalClosed(row["state"])
            if row["expires_at"] <= now:
                self._expire(conn, now, row)
                raise ApprovalClosed("expired")
            a = _decode(row)
            final = None
            if answer == "approve":
                final = dict(a["payload"].get("fields") or {})
                if fields:
                    unknown = set(fields) - set(final)
                    if unknown:
                        raise ApprovalError(f"unknown fields: {', '.join(sorted(unknown))}")
                    final.update(fields)
            state = "approved" if answer == "approve" else "rejected"
            conn.execute(
                "UPDATE approvals SET state = ?, answer = ?, decided_by = ?, decided_at = ?, token_hash = NULL,"
                " remind_at = NULL, updated_at = ? WHERE id = ?",
                (state, _dumps(final) if final is not None else None, by[:60], now, now, approval_id),
            )
            insert_event(conn, now, f"approval.{state}", job_id=a["job_id"], step=a["step"], src="approvals",
                         dst=a["plugin"], data={"approval": approval_id, "by": by[:60],
                                                "edited": bool(fields) and answer == "approve"})
            self._resume(conn, now, a["job_id"], approval_id)
            return _decode(conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone())

        return await self.store.write(fn)

    def _resume(self, conn: sqlite3.Connection, now: float, job_id: str | None, approval_id: str) -> None:
        if not job_id:
            return
        job = self.jobs._get(conn, job_id)
        # Still running (the worker has not parked it yet)? JobStore.wait sees the decision and requeues it.
        if job.state is JobState.WAITING and job.wait_reason == f"approval:{approval_id}":
            self.jobs._move(conn, job, JobState.QUEUED, now, src_component="approvals", wait_reason=None,
                            run_after=now, priority=max(job.priority, RESUME_PRIORITY),
                            event_data={"reason": "approval decided"})

    def _expire(self, conn: sqlite3.Connection, now: float, row: sqlite3.Row) -> None:
        conn.execute("UPDATE approvals SET state = 'expired', token_hash = NULL, remind_at = NULL,"
                     " decided_by = 'timeout', decided_at = ?, updated_at = ? WHERE id = ?", (now, now, row["id"]))
        insert_event(conn, now, "approval.expired", job_id=row["job_id"], step=row["step"], src="approvals",
                     dst=row["plugin"], data={"approval": row["id"]})
        self._resume(conn, now, row["job_id"], row["id"])

    # -------------------------------------------------------------- watchdog

    async def tick(self) -> dict[str, int]:
        """Send due reminders and expire old approvals. Runs on the watchdog."""

        def fn(conn: sqlite3.Connection) -> dict[str, int]:
            now = self.clock()
            reminded = expired = 0
            for row in conn.execute("SELECT * FROM approvals WHERE state = 'pending' AND expires_at <= ?",
                                    (now,)).fetchall():
                self._expire(conn, now, row)
                expired += 1
            for row in conn.execute("SELECT * FROM approvals WHERE state = 'pending' AND remind_at <= ?",
                                    (now,)).fetchall():
                a = _decode(row)
                add_message(conn, now, "ntfy", self._message(a, reminder=True),
                            dedupe_key=f"approval-remind:{a['id']}", job_id=a["job_id"])
                conn.execute("UPDATE approvals SET remind_at = NULL, updated_at = ? WHERE id = ?", (now, a["id"]))
                insert_event(conn, now, "approval.reminded", job_id=a["job_id"], src="approvals", dst="ntfy",
                             data={"approval": a["id"]})
                reminded += 1
            return {"reminded": reminded, "expired": expired}

        return await self.store.write(fn)

    # -------------------------------------------------------------- the phone is back

    async def push_waiting(self, dedupe: str) -> int:
        """Queue one message about everything still waiting (the phone just came back on Tailscale).
        One approval: its card again with fresh buttons. Several: a count and titles, opening Helios."""

        def fn(conn: sqlite3.Connection) -> int:
            now = self.clock()
            rows = [_decode(r) for r in conn.execute(
                "SELECT * FROM approvals WHERE state = 'pending' ORDER BY created_at").fetchall()]
            if not rows:
                return 0
            if len(rows) == 1:
                msg = self._message(rows[0])
                msg["title"] = f"Still waiting: {rows[0]['plugin']}: {rows[0]['title']}"
            else:
                base = self.cfg.approvals.public_url
                lines = [f"• {a['plugin']}: {a['title']}" for a in rows[:8]]
                if len(rows) > 8:
                    lines.append(f"… and {len(rows) - 8} more")
                msg = ntfy_message(f"{len(rows)} approvals waiting", "\n".join(lines), priority="high",
                                   tags=["inbox_tray"], click=f"{base}/" if base else None,
                                   actions=[{"action": "view", "label": "Open Helios", "url": f"{base}/"}]
                                   if base else None)
            if add_message(conn, now, "ntfy", msg, dedupe_key=dedupe) is None:
                return 0
            insert_event(conn, now, "approval.pushed", src="approvals", dst="ntfy",
                         data={"count": len(rows), "reason": "phone back online"})
            return len(rows)

        return await self.store.write(fn)

    # -------------------------------------------------------------- reads

    async def get(self, approval_id: str) -> dict[str, Any]:
        def fn(conn: sqlite3.Connection) -> dict[str, Any]:
            row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
            if row is None:
                raise ApprovalNotFound(approval_id)
            return _decode(row)

        return await self.store.read(fn)

    async def list(self, state: str | None = None, job_id: str | None = None, limit: int = 100) -> list[dict]:
        def fn(conn: sqlite3.Connection) -> list[dict]:
            where, args = [], []
            if state:
                where.append("state = ?")
                args.append(state)
            if job_id:
                where.append("job_id = ?")
                args.append(job_id)
            sql = "SELECT * FROM approvals" + (f" WHERE {' AND '.join(where)}" if where else "")
            rows = conn.execute(sql + " ORDER BY created_at DESC LIMIT ?", (*args, limit)).fetchall()
            return [_decode(r) for r in rows]

        return await self.store.read(fn)

    async def verify_link(self, approval_id: str, token: str) -> dict[str, Any]:
        """For the phone page: the approval, if the token is right (even when already decided, to show that)."""
        a = await self.get(approval_id)
        if not hmac.compare_digest(token, self.token(approval_id)):
            raise BadToken("this link is not valid")
        return a
