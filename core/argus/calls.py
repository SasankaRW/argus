"""The model call log (C0): every call a worker made through the router, kept on Argus for Helios.

Models > Calls lists them (newest first, filters); each job's Inspector shows its own. A record has the model, the
prompt as sent (the playbook with lessons and examples, and the messages), the raw reply, the verdict (ok,
rejected and why, or the error), the time taken and tokens. Pictures are never kept, only how many there were.
Kept `models.call_log_days` days and at most `models.call_log_mb` MB, oldest first.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

FIELDS = ("tier", "provider", "model", "attempt", "verdict", "reason", "latency_ms", "tokens_in", "tokens_out",
          "images", "system", "prompt", "reply", "worker", "step")
VERDICTS = ("ok", "rejected", "error")
PRUNE_EVERY = 50  # inserts between prunes


def add_call(conn: sqlite3.Connection, now: float, job_id: str, body: dict[str, Any], *, keep_days: float,
             max_mb: int, prompts: bool = True) -> int:
    rec = {k: body.get(k) for k in FIELDS}
    if rec["verdict"] not in VERDICTS:
        rec["verdict"] = "error"
    if not prompts:
        rec["system"] = rec["prompt"] = None
    for k in ("system", "prompt", "reply", "reason"):
        if rec[k] is not None and not isinstance(rec[k], str):
            rec[k] = json.dumps(rec[k], ensure_ascii=False, default=str)
    size = sum(len(rec[k] or "") for k in ("system", "prompt", "reply"))
    job = conn.execute("SELECT plugin, workflow FROM jobs WHERE id = ?", (job_id,)).fetchone()
    cur = conn.execute(
        "INSERT INTO model_calls (created_at, job_id, plugin, workflow, step, worker, tier, provider, model, attempt,"
        " verdict, reason, latency_ms, tokens_in, tokens_out, images, system, prompt, reply, bytes)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (now, job_id, job["plugin"] if job else None, job["workflow"] if job else None, rec["step"], rec["worker"],
         rec["tier"], rec["provider"], rec["model"], rec["attempt"], rec["verdict"], rec["reason"], rec["latency_ms"],
         rec["tokens_in"], rec["tokens_out"], int(rec["images"] or 0), rec["system"], rec["prompt"], rec["reply"],
         size))
    cid = int(cur.lastrowid or 0)
    if cid % PRUNE_EVERY == 0:
        prune(conn, now, keep_days, max_mb)
    return cid


def prune(conn: sqlite3.Connection, now: float, keep_days: float, max_mb: int) -> int:
    """Drop calls older than `keep_days`, then the oldest until the log is under `max_mb`."""
    n = conn.execute("DELETE FROM model_calls WHERE created_at < ?", (now - keep_days * 86400,)).rowcount
    total = conn.execute("SELECT COALESCE(SUM(bytes), 0) FROM model_calls").fetchone()[0]
    limit = max_mb * 1024 * 1024
    if total > limit:
        cut, run = None, total
        for r in conn.execute("SELECT id, bytes FROM model_calls ORDER BY id"):
            run -= r["bytes"]
            cut = r["id"]
            if run <= limit * 0.9:  # some room, so it doesn't prune on every insert
                break
        if cut is not None:
            n += conn.execute("DELETE FROM model_calls WHERE id <= ?", (cut,)).rowcount
    return n


def row_json(r: sqlite3.Row, full: bool = False) -> dict[str, Any]:
    out = {k: r[k] for k in ("id", "created_at", "job_id", "plugin", "workflow", "step", "worker", "tier",
                             "provider", "model", "attempt", "verdict", "reason", "latency_ms", "tokens_in",
                             "tokens_out", "images")}
    if full:
        try:
            prompt = json.loads(r["prompt"]) if r["prompt"] else None
        except ValueError:
            prompt = r["prompt"]
        out.update({"system": r["system"], "prompt": prompt, "reply": r["reply"]})
    else:  # the list: the start of the reply is enough
        out["reply_start"] = (r["reply"] or "")[:160]
    return out


def list_calls(conn: sqlite3.Connection, *, limit: int = 100, before: int | None = None, model: str | None = None,
               plugin: str | None = None, verdict: str | None = None, q: str | None = None,
               job: str | None = None) -> list[dict[str, Any]]:
    where, args = [], []
    if before:
        where.append("id < ?")
        args.append(before)
    if model:
        where.append("(model = ? OR tier = ?)")
        args += [model, model]
    if plugin:
        where.append("plugin = ?")
        args.append(plugin)
    if verdict == "problems":
        where.append("verdict != 'ok'")
    elif verdict in VERDICTS:
        where.append("verdict = ?")
        args.append(verdict)
    if job:
        where.append("job_id = ?")
        args.append(job)
    if q:
        where.append("(system LIKE ? OR prompt LIKE ? OR reply LIKE ? OR reason LIKE ?)")
        args += [f"%{q}%"] * 4
    sql = "SELECT * FROM model_calls" + (" WHERE " + " AND ".join(where) if where else "")
    order = " ORDER BY id" if job else " ORDER BY id DESC"
    rows = conn.execute(sql + order + " LIMIT ?", (*args, limit)).fetchall()
    return [row_json(r, full=bool(job)) for r in rows]


def summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """For the filters: the models and plugins seen, and how big the log is."""
    models = [r[0] for r in conn.execute("SELECT DISTINCT COALESCE(model, tier) FROM model_calls WHERE "
                                         "COALESCE(model, tier) IS NOT NULL ORDER BY 1")]
    plugins = [r[0] for r in conn.execute("SELECT DISTINCT plugin FROM model_calls WHERE plugin IS NOT NULL "
                                          "ORDER BY 1")]
    n, size = conn.execute("SELECT COUNT(*), COALESCE(SUM(bytes), 0) FROM model_calls").fetchone()
    return {"models": models, "plugins": plugins, "count": n, "mb": round(size / 1048576, 1)}
