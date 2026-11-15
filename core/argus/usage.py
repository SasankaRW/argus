"""Month-by-month use per plugin (F1): model calls, hand-ups to a bigger model, and job runs.

`/models/usage` only reaches back as far as the events do (`events.retention_days`, 90 by default). `rollup` copies
the monthly counts into `usage_monthly` before the events are pruned; the table is kept `KEEP_MONTHS` months.
Counts only ever grow (raw data is only ever deleted, never added back in the past), so a rollup takes the larger
of the stored and the recounted number: a month that is partly pruned keeps what an earlier rollup saw.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import time
from typing import Any

KEEP_MONTHS = 60
MAX_MONTHS = 60
_MONTH = "strftime('%Y-%m', {col}, 'unixepoch', 'localtime')"


def rollup(conn: sqlite3.Connection, now: float | None = None) -> int:
    """Fold the events and finished jobs still on disk into `usage_monthly`; drop months past `KEEP_MONTHS`.
    Returns the number of month/plugin rows written."""
    now = time.time() if now is None else now
    counts: dict[tuple[str, str], list[int]] = {}
    for r in conn.execute(
        f"SELECT {_MONTH.format(col='e.at')} AS month, COALESCE(j.plugin, e.from_component) AS plugin,"
        " SUM(e.kind = 'model.request') AS calls, SUM(e.kind = 'model.escalated') AS esc"
        " FROM events e LEFT JOIN jobs j ON j.id = e.job_id"
        " WHERE e.kind IN ('model.request', 'model.escalated') GROUP BY 1, 2"
    ):
        if r["plugin"]:
            counts[(r["month"], r["plugin"])] = [r["calls"], r["esc"], 0, 0]
    for r in conn.execute(
        f"SELECT {_MONTH.format(col='finished_at')} AS month, plugin, COUNT(*) AS n,"
        " SUM(state IN ('dead', 'cancelled')) AS failed FROM jobs"
        " WHERE finished_at IS NOT NULL AND state IN ('succeeded', 'dead', 'cancelled') GROUP BY 1, 2"
    ):
        row = counts.setdefault((r["month"], r["plugin"]), [0, 0, 0, 0])
        row[2], row[3] = r["n"], r["failed"]
    for (month, plugin), (calls, esc, runs, failed) in counts.items():
        conn.execute(
            "INSERT INTO usage_monthly (month, plugin, model_calls, escalations, job_runs, job_failed)"
            " VALUES (?,?,?,?,?,?) ON CONFLICT (month, plugin) DO UPDATE SET"
            " model_calls = MAX(model_calls, excluded.model_calls),"
            " escalations = MAX(escalations, excluded.escalations),"
            " job_runs = MAX(job_runs, excluded.job_runs), job_failed = MAX(job_failed, excluded.job_failed)",
            (month, plugin, calls, esc, runs, failed))
    conn.execute("DELETE FROM usage_monthly WHERE month < ?", (_months_back(now, KEEP_MONTHS),))
    return len(counts)


def _months_back(now: float, n: int) -> str:
    d = dt.datetime.fromtimestamp(now)
    idx = d.year * 12 + d.month - 1 - (n - 1)
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def monthly(conn: sqlite3.Connection, months: int = 12, now: float | None = None) -> dict[str, Any]:
    """The last `months` months (this one included), oldest first, each with its plugins busiest first."""
    now = time.time() if now is None else now
    months = max(1, min(int(months), MAX_MONTHS))
    first = _months_back(now, months)
    out: dict[str, dict[str, Any]] = {}
    for r in conn.execute("SELECT * FROM usage_monthly WHERE month >= ? ORDER BY month, plugin", (first,)):
        m = out.setdefault(r["month"], {"month": r["month"], "model_calls": 0, "job_runs": 0, "plugins": []})
        m["model_calls"] += r["model_calls"]
        m["job_runs"] += r["job_runs"]
        m["plugins"].append({k: r[k] for k in ("plugin", "model_calls", "escalations", "job_runs", "job_failed")})
    for m in out.values():
        m["plugins"].sort(key=lambda p: (-p["model_calls"] - p["job_runs"], p["plugin"]))
    return {"months": months, "from": first, "data": [out[k] for k in sorted(out)]}
