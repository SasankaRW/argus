"""The guidance loop: the local models get better from their own mistakes, with you deciding.

1. Samples. Every answer a plugin's model call gives (ctx.llm) is kept: the playbook, the input, the answer, which
   tier answered and whether the first tier's answer was rejected (escalated). Last `guidance.keep_samples` per
   playbook.
2. Your verdicts. In Helios (a job's "Model answers") you mark an answer Correct or Wrong (with what it should have
   been). Correct ones are the playbook's eval set; wrong ones and escalations are what the review looks at.
3. Review (nightly at `guidance.at`, or "Review now"): for each playbook with new mistakes, Claude reads the playbook,
   the mistakes and the right answers, and writes a few short lessons. The eval set is replayed on the local tier
   with and without them; you get the lessons and the before/after scores to approve (Helios and the phone).
4. Approved lessons are appended to that playbook for that plugin from then on ("Lessons from earlier mistakes").
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from typing import Any


def playbook_key(plugin: str, text: str) -> str:
    return f"{plugin}:{hashlib.sha1(text.encode()).hexdigest()[:12]}"


def add_sample(conn: sqlite3.Connection, now: float, *, job_id: str, plugin: str, playbook: str,
               schema: Any, input: Any, output: Any, tier: str | None, escalated: bool, keep: int) -> int:
    key = playbook_key(plugin, playbook)
    name = next((ln.strip() for ln in playbook.strip().splitlines() if ln.strip()), "")[:120]
    conn.execute("INSERT INTO playbooks (key, plugin, name, text, schema, first_seen, last_seen) VALUES (?,?,?,?,?,?,?)"
                 " ON CONFLICT(key) DO UPDATE SET last_seen = excluded.last_seen",
                 (key, plugin, name, playbook, json.dumps(schema) if schema else None, now, now))
    lessons = conn.execute("SELECT id FROM lessons WHERE playbook = ? AND state = 'active'", (key,)).fetchone()
    cur = conn.execute("INSERT INTO samples (job_id, plugin, playbook, input, output, tier, escalated, created_at,"
                       " lessons_id) VALUES (?,?,?,?,?,?,?,?,?)",
                       (job_id, plugin, key, _j(input), _j(output), tier, int(escalated), now,
                        lessons[0] if lessons else None))
    # keep the newest `keep` without a verdict; marked ones are the eval set and stay
    conn.execute("DELETE FROM samples WHERE playbook = ? AND verdict IS NULL AND id NOT IN (SELECT id FROM samples"
                 " WHERE playbook = ? AND verdict IS NULL ORDER BY id DESC LIMIT ?)", (key, key, keep))
    return int(cur.lastrowid or 0)


def _j(v: Any) -> str:
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)


def _p(s: str | None) -> Any:
    if s is None:
        return None
    try:
        return json.loads(s)
    except ValueError:
        return s


def sample_json(r: sqlite3.Row, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """One kept answer. With `conn`: also the lessons that were in force when it was given ("lessons used")."""
    out = {"id": r["id"], "job_id": r["job_id"], "plugin": r["plugin"], "playbook": r["playbook"],
           "input": _p(r["input"]), "output": _p(r["output"]), "tier": r["tier"], "escalated": bool(r["escalated"]),
           "verdict": r["verdict"], "correction": _p(r["correction"]), "created_at": r["created_at"]}
    lid = r["lessons_id"] if "lessons_id" in r.keys() else None
    if conn is not None and lid:
        lr = conn.execute("SELECT id, text, decided_at FROM lessons WHERE id = ?", (lid,)).fetchone()
        if lr is not None:
            out["lessons"] = {"id": lr["id"], "count": sum(1 for ln in lr["text"].splitlines() if ln.strip()),
                              "approved_at": lr["decided_at"], "text": lr["text"]}
    return out


def trend(conn: sqlite3.Connection, key: str, now: float, weeks: int = 8) -> dict[str, Any]:
    """Is it learning? Per week: answers, how many the first model got right (not escalated, not marked wrong),
    escalations, answers marked wrong; and when lessons were approved (to see before / after)."""
    start = now - weeks * 7 * 86400
    rows = conn.execute("SELECT created_at, escalated, verdict, lessons_id FROM samples WHERE playbook = ? AND"
                        " created_at >= ? ORDER BY created_at", (key, start)).fetchall()
    out = []
    for w in range(weeks):
        a, b = start + w * 7 * 86400, start + (w + 1) * 7 * 86400
        week = [r for r in rows if a <= r["created_at"] < b]
        ok = sum(1 for r in week if not r["escalated"] and r["verdict"] != "wrong")
        out.append({"from": a, "answers": len(week), "first_right": ok,
                    "escalated": sum(1 for r in week if r["escalated"]),
                    "wrong": sum(1 for r in week if r["verdict"] == "wrong"),
                    "with_lessons": sum(1 for r in week if r["lessons_id"]),
                    "rate": round(ok / len(week), 3) if week else None})
    approved = [{"id": r["id"], "at": r["decided_at"], "state": r["state"]} for r in conn.execute(
        "SELECT id, decided_at, state FROM lessons WHERE playbook = ? AND decided_at IS NOT NULL AND"
        " state IN ('active', 'replaced') AND decided_at >= ? ORDER BY decided_at", (key, start))]
    return {"weeks": out, "lessons": approved}


def set_verdict(conn: sqlite3.Connection, sample_id: int, verdict: str | None, correction: Any = None) -> bool:
    return conn.execute("UPDATE samples SET verdict = ?, correction = ? WHERE id = ?",
                        (verdict, _j(correction) if correction not in (None, "") else None, sample_id)).rowcount > 0


def active_lessons(conn: sqlite3.Connection, plugin: str) -> dict[str, str]:
    """{playbook key: lessons text} in force for a plugin (sent to workers with each job)."""
    return {r["playbook"]: r["text"] for r in conn.execute(
        "SELECT l.playbook, l.text FROM lessons l JOIN playbooks p ON p.key = l.playbook"
        " WHERE p.plugin = ? AND l.state = 'active'", (plugin,))}


def overview(conn: sqlite3.Connection, plugin: str | None = None) -> list[dict[str, Any]]:
    """Per playbook: samples, escalations, verdicts, the lessons in force and waiting."""
    where = " WHERE plugin = ?" if plugin else ""
    rows = conn.execute(f"SELECT * FROM playbooks{where} ORDER BY last_seen DESC", (plugin,) if plugin else ()
                        ).fetchall()
    out = []
    for p in rows:
        c = conn.execute("SELECT COUNT(*), SUM(escalated), SUM(verdict = 'correct'), SUM(verdict = 'wrong'),"
                         " SUM(verdict IS NULL AND escalated = 1 AND reviewed_at IS NULL)"
                         " FROM samples WHERE playbook = ?", (p["key"],)).fetchone()
        lessons = [{"id": r["id"], "text": r["text"], "state": r["state"], "evals": _p(r["evals"]),
                    "created_at": r["created_at"]} for r in conn.execute(
            "SELECT * FROM lessons WHERE playbook = ? AND state IN ('active', 'proposed') ORDER BY id", (p["key"],))]
        run = conn.execute("SELECT passed, total, created_at FROM eval_runs WHERE playbook = ?"
                           " AND why != 'without lessons' ORDER BY id DESC LIMIT 1", (p["key"],)).fetchone()
        out.append({"key": p["key"], "plugin": p["plugin"], "name": p["name"], "samples": c[0] or 0,
                    "escalated": c[1] or 0, "correct": c[2] or 0, "wrong": c[3] or 0, "to_review": c[4] or 0,
                    "lessons": lessons, "last_seen": p["last_seen"],
                    "last_run": {"passed": run[0], "total": run[1], "at": run[2]} if run else None})
    return out


def review_inputs(conn: sqlite3.Connection, max_per_playbook: int = 12) -> list[dict[str, Any]]:
    """Playbooks with something new to learn from: wrong verdicts or escalations not reviewed yet."""
    out = []
    for p in conn.execute("SELECT * FROM playbooks").fetchall():
        mistakes = conn.execute(
            "SELECT * FROM samples WHERE playbook = ? AND reviewed_at IS NULL AND (verdict = 'wrong' OR"
            " (verdict IS NULL AND escalated = 1)) ORDER BY id DESC LIMIT ?", (p["key"], max_per_playbook)).fetchall()
        if not mistakes:
            continue
        correct = conn.execute("SELECT * FROM samples WHERE playbook = ? AND (verdict = 'correct' OR (verdict IS NULL"
                               " AND escalated = 0)) ORDER BY verdict IS NULL, id DESC LIMIT 20",
                               (p["key"],)).fetchall()
        current = conn.execute("SELECT text FROM lessons WHERE playbook = ? AND state = 'active'",
                               (p["key"],)).fetchone()
        out.append({"key": p["key"], "plugin": p["plugin"], "name": p["name"], "playbook": p["text"],
                    "schema": _p(p["schema"]), "lessons": current[0] if current else "",
                    "mistakes": [sample_json(r) for r in mistakes], "evals": [sample_json(r) for r in correct]})
    return out


def mark_reviewed(conn: sqlite3.Connection, ids: list[int], now: float) -> None:
    if ids:
        conn.execute(f"UPDATE samples SET reviewed_at = ? WHERE id IN ({','.join('?' * len(ids))})", (now, *ids))


def propose(conn: sqlite3.Connection, now: float, key: str, text: str, evals: dict, job_id: str | None) -> int:
    conn.execute("UPDATE lessons SET state = 'replaced', decided_at = ? WHERE playbook = ? AND state = 'proposed'",
                 (now, key))
    cur = conn.execute("INSERT INTO lessons (playbook, text, state, evals, job_id, created_at) VALUES (?,?,?,?,?,?)",
                       (key, text.strip()[:3000], "proposed", json.dumps(evals), job_id, now))
    return int(cur.lastrowid or 0)


def decide(conn: sqlite3.Connection, lesson_id: int, approve: bool, now: float) -> dict[str, Any] | None:
    r = conn.execute("SELECT * FROM lessons WHERE id = ?", (lesson_id,)).fetchone()
    if r is None or r["state"] != "proposed":
        return None
    if approve:
        conn.execute("UPDATE lessons SET state = 'replaced', decided_at = ? WHERE playbook = ? AND state = 'active'",
                     (now, r["playbook"]))
    conn.execute("UPDATE lessons SET state = ?, decided_at = ? WHERE id = ?",
                 ("active" if approve else "rejected", now, lesson_id))
    return {"id": lesson_id, "state": "active" if approve else "rejected", "playbook": r["playbook"]}


def drop_active(conn: sqlite3.Connection, lesson_id: int) -> bool:
    return conn.execute("UPDATE lessons SET state = 'replaced', decided_at = ? WHERE id = ? AND state = 'active'",
                        (time.time(), lesson_id)).rowcount > 0


def eval_samples(conn: sqlite3.Connection, key: str, limit: int = 20) -> list[sqlite3.Row]:
    """The eval set: answers you marked correct, else ones the first tier got right (newest first)."""
    return conn.execute("SELECT * FROM samples WHERE playbook = ? AND (verdict = 'correct' OR (verdict IS NULL"
                        " AND escalated = 0)) ORDER BY verdict IS NULL, id DESC LIMIT ?", (key, limit)).fetchall()


def playbook_input(conn: sqlite3.Connection, key: str) -> dict[str, Any] | None:
    """What a worker needs to replay this playbook: its text, schema, the lessons in force and the eval set."""
    p = conn.execute("SELECT * FROM playbooks WHERE key = ?", (key,)).fetchone()
    if p is None:
        return None
    current = conn.execute("SELECT text FROM lessons WHERE playbook = ? AND state = 'active'", (key,)).fetchone()
    return {"key": key, "plugin": p["plugin"], "name": p["name"], "playbook": p["text"], "schema": _p(p["schema"]),
            "lessons": current[0] if current else "", "evals": [sample_json(r) for r in eval_samples(conn, key)]}


def record_run(conn: sqlite3.Connection, now: float, key: str, run: dict[str, Any], why: str,
               job_id: str | None) -> int:
    cur = conn.execute("INSERT INTO eval_runs (playbook, passed, total, failed, tier, why, job_id, created_at)"
                       " VALUES (?,?,?,?,?,?,?,?)",
                       (key, int(run.get("passed") or 0), int(run.get("total") or 0),
                        json.dumps(run.get("failed") or []), run.get("tier"), why, job_id, now))
    return int(cur.lastrowid or 0)


def runs(conn: sqlite3.Connection, key: str, limit: int = 30) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM eval_runs WHERE playbook = ? ORDER BY id DESC LIMIT ?", (key, limit)).fetchall()
    return [{"id": r["id"], "passed": r["passed"], "total": r["total"], "failed": _p(r["failed"]) or [],
             "tier": r["tier"], "why": r["why"], "job_id": r["job_id"], "at": r["created_at"]} for r in reversed(rows)]
