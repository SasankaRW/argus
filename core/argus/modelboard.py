"""The model board: which model tiers may be called right now, shared by every worker.

Workers make the model calls; argusd keeps the shared state so all workers obey it:

- **Circuit breaker per tier.** After `breaker_failures` failed calls in a row (timeouts, errors, model
  missing) the tier is *open* for `breaker_open_seconds`: calls are refused at once and steps move on to the
  next tier instead of piling up. Then one trial call is let through (*half open*); success closes it.
- **Daily Claude budget.** Every call to a Claude tier reserves one unit of `claude.calls_per_day` first.

Bad answers (invalid JSON, failed checks) are not breaker failures: the model is up, it was just wrong.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .config import Config
from .db import Store
from .events import insert_event
from .registry import _upsert_component

CLAUDE_BUDGET = "claude_calls"


def component_id(tier: str) -> str:
    return tier.lower()


@dataclass(frozen=True)
class Permit:
    allowed: bool
    reason: str | None = None
    retry_after: float | None = None  # seconds until the tier may be tried again

    def as_dict(self) -> dict[str, Any]:
        return {"allowed": self.allowed, "reason": self.reason, "retry_after": self.retry_after}


class ModelBoard:
    def __init__(self, store: Store, cfg: Config, clock: Callable[[], float] = time.time):
        self.store = store
        self.cfg = cfg
        self.clock = clock
        self.plugin_caps: dict[str, int] = {}  # plugin -> claude_calls_per_day from its manifest

    def day(self) -> str:
        return time.strftime("%Y-%m-%d", time.localtime(self.clock()))

    def label(self, tier: str) -> str:
        t = self.cfg.models.tiers[tier]
        if t.label:
            return t.label
        return f"{tier} · {t.model}" if t.provider == "ollama" else f"{tier} · Claude"

    # -------------------------------------------------------------- setup

    async def register(self) -> None:
        """Put every configured tier on the map and make sure it has a state row."""

        def fn(conn: sqlite3.Connection) -> None:
            now = self.clock()
            for tier, t in self.cfg.models.tiers.items():
                meta = {"tier": tier, "provider": t.provider, "model": t.model}
                if t.provider == "claude":
                    meta["calls_per_day"] = self.cfg.claude.calls_per_day
                _upsert_component(conn, now, component_id(tier), "model", self.label(tier), t.group, meta)
                conn.execute(
                    "INSERT OR IGNORE INTO model_state (tier, created_at, updated_at) VALUES (?, ?, ?)",
                    (tier, now, now),
                )

        await self.store.write(fn)

    # -------------------------------------------------------------- permit / report

    def _row(self, conn: sqlite3.Connection, tier: str, now: float) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM model_state WHERE tier = ?", (tier,)).fetchone()
        if row is None:
            conn.execute("INSERT INTO model_state (tier, created_at, updated_at) VALUES (?, ?, ?)", (tier, now, now))
            row = conn.execute("SELECT * FROM model_state WHERE tier = ?", (tier,)).fetchone()
        return row

    async def permit(self, tier: str, *, worker: str | None = None, job_id: str | None = None) -> Permit:
        """May `tier` be called now? For Claude tiers this also reserves one call from today's budget."""
        if tier not in self.cfg.models.tiers:
            return Permit(False, f"unknown tier {tier}")
        provider = self.cfg.models.tiers[tier].provider

        def fn(conn: sqlite3.Connection) -> Permit:
            now = self.clock()
            row = self._row(conn, tier, now)
            # Half open lets exactly one trial call through; the others wait until it reports (or its time
            # runs out, in case that worker died mid-call).
            trial = (self.cfg.claude.timeout_seconds if provider == "claude" else self.cfg.ollama.timeout_seconds) + 30
            if row["state"] == "open":
                if row["opened_until"] and row["opened_until"] > now:
                    return Permit(False, "breaker open", round(row["opened_until"] - now, 1))
                conn.execute("UPDATE model_state SET state = 'half_open', opened_until = ?, updated_at = ?"
                             " WHERE tier = ?", (now + trial, now, tier))
                insert_event(conn, now, "model.breaker_half_open", job_id=job_id, src=component_id(tier),
                             data={"tier": tier})
            elif row["state"] == "half_open":
                if row["opened_until"] and row["opened_until"] > now:
                    return Permit(False, "trial call in progress", round(row["opened_until"] - now, 1))
                conn.execute("UPDATE model_state SET opened_until = ?, updated_at = ? WHERE tier = ?",
                             (now + trial, now, tier))
            pkey = None
            if provider == "claude" and job_id:
                job = conn.execute("SELECT plugin FROM jobs WHERE id = ?", (job_id,)).fetchone()
                pcap = self.plugin_caps.get(job["plugin"]) if job else None
                if pcap is not None:
                    pkey = f"{CLAUDE_BUDGET}:{job['plugin']}"
                    row = conn.execute("SELECT used FROM budget WHERE day = ? AND key = ?",
                                       (self.day(), pkey)).fetchone()
                    if (row[0] if row else 0) >= pcap:
                        return Permit(False, f"plugin's daily Claude calls used up ({pcap})")
            if provider == "claude":
                cap = self.cfg.claude.calls_per_day
                day = self.day()
                used = conn.execute("SELECT used FROM budget WHERE day = ? AND key = ?",
                                    (day, CLAUDE_BUDGET)).fetchone()
                used = used[0] if used else 0
                if used >= cap:
                    return Permit(False, f"daily Claude cap reached ({cap})")
                for key in (CLAUDE_BUDGET, pkey):
                    if key:
                        conn.execute(
                            "INSERT INTO budget (day, key, used, created_at, updated_at) VALUES (?, ?, 1, ?, ?)"
                            " ON CONFLICT(day, key) DO UPDATE SET used = used + 1, updated_at = excluded.updated_at",
                            (day, key, now, now),
                        )
            return Permit(True)

        return await self.store.write(fn)

    async def report(self, tier: str, ok: bool, *, latency_ms: float | None = None, error: str | None = None,
                     job_id: str | None = None) -> dict[str, Any]:
        """Record how a call went. Returns the tier's state afterwards."""
        m = self.cfg.models

        def fn(conn: sqlite3.Connection) -> dict[str, Any]:
            now = self.clock()
            row = self._row(conn, tier, now)
            if ok:
                if row["state"] != "closed":
                    insert_event(conn, now, "model.breaker_closed", job_id=job_id, src=component_id(tier),
                                 data={"tier": tier})
                conn.execute(
                    "UPDATE model_state SET state = 'closed', consecutive_failures = 0, opened_until = NULL,"
                    " calls = calls + 1, last_latency_ms = ?, updated_at = ? WHERE tier = ?",
                    (latency_ms, now, tier),
                )
            else:
                fails = row["consecutive_failures"] + 1
                trip = row["state"] == "half_open" or fails >= m.breaker_failures
                state = "open" if trip else row["state"]
                until = now + m.breaker_open_seconds if trip else row["opened_until"]
                conn.execute(
                    "UPDATE model_state SET state = ?, consecutive_failures = ?, opened_until = ?, last_error = ?,"
                    " calls = calls + 1, failures = failures + 1, last_latency_ms = ?, updated_at = ? WHERE tier = ?",
                    (state, fails, until, (error or "")[:500], latency_ms, now, tier),
                )
                if trip:
                    insert_event(conn, now, "model.breaker_opened", job_id=job_id, src=component_id(tier),
                                 data={"tier": tier, "failures": fails, "error": (error or "")[:200],
                                       "seconds": m.breaker_open_seconds})
            return dict(conn.execute("SELECT * FROM model_state WHERE tier = ?", (tier,)).fetchone())

        return await self.store.write(fn)

    # -------------------------------------------------------------- reads

    async def snapshot(self) -> dict[str, Any]:
        def fn(conn: sqlite3.Connection) -> dict[str, Any]:
            now = self.clock()
            states = {r["tier"]: dict(r) for r in conn.execute("SELECT * FROM model_state").fetchall()}
            used = conn.execute("SELECT used FROM budget WHERE day = ? AND key = ?",
                                (self.day(), CLAUDE_BUDGET)).fetchone()
            tiers = []
            for tier, t in self.cfg.models.tiers.items():
                st = states.get(tier, {})
                state = st.get("state", "closed")
                if state == "open" and (st.get("opened_until") or 0) <= now:
                    state = "half_open"  # due for a trial call
                tiers.append({
                    "tier": tier, "component": component_id(tier), "label": self.label(tier),
                    "provider": t.provider, "model": t.model, "state": state,
                    "retry_after": round(st["opened_until"] - now, 1) if state == "open" else None,
                    "calls": st.get("calls", 0), "failures": st.get("failures", 0),
                    "last_error": st.get("last_error"), "last_latency_ms": st.get("last_latency_ms"),
                })
            return {
                "tiers": tiers,
                "chain": list(self.cfg.models.chain),
                "claude": {"calls_today": used[0] if used else 0, "calls_per_day": self.cfg.claude.calls_per_day},
            }

        return await self.store.read(fn)

    def worker_config(self) -> dict[str, Any]:
        """What a worker needs to make model calls itself."""
        m, c, o = self.cfg.models, self.cfg.claude, self.cfg.ollama
        return {
            "tiers": {k: v.model_dump() for k, v in m.tiers.items()},
            "chain": list(m.chain),
            "attempts_per_tier": m.attempts_per_tier,
            "ollama": {"url": o.url, "timeout_seconds": o.timeout_seconds, "keep_alive": o.keep_alive},
            "claude": {"command": list(c.command), "args": list(c.args), "timeout_seconds": c.timeout_seconds},
        }
