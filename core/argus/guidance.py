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
5. Your fixes count without a click (`implicit`): an Undo or Wrong on a job's change, a file you moved back, renamed
   again or moved elsewhere (a worker looks every few hours: `followups`), "no, I meant ..." to Ari.
6. Experience memory (`examples`): answers you confirmed, fixed, or left alone for a day are worked examples; the
   worker gives the model the 2-3 most similar to the input. Examples that keep leading to mistakes are dropped
   (`harm`); only verified answers are added (research: storing everything makes agents worse).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from typing import Any


def playbook_key(plugin: str, text: str) -> str:
    return f"{plugin}:{hashlib.sha1(text.encode()).hexdigest()[:12]}"


EXAMPLES_KEPT = 100   # confirmed answers kept per playbook as worked examples (newest win)
EXAMPLES_SENT = 60    # sent to the worker with each job (it picks the most similar few)
HARM_LIMIT = 2        # examples that led to this many wrong answers are dropped (yours: one more)
KEEP_AFTER = 86400    # a change left alone this long counts as a yes


def add_sample(conn: sqlite3.Connection, now: float, *, job_id: str, plugin: str, playbook: str,
               schema: Any, input: Any, output: Any, tier: str | None, escalated: bool, keep: int,
               subject: str | None = None, used: list[int] | None = None) -> int:
    key = playbook_key(plugin, playbook)
    name = next((ln.strip() for ln in playbook.strip().splitlines() if ln.strip()), "")[:120]
    conn.execute("INSERT INTO playbooks (key, plugin, name, text, schema, first_seen, last_seen) VALUES (?,?,?,?,?,?,?)"
                 " ON CONFLICT(key) DO UPDATE SET last_seen = excluded.last_seen",
                 (key, plugin, name, playbook, json.dumps(schema) if schema else None, now, now))
    lessons = conn.execute("SELECT id FROM lessons WHERE playbook = ? AND state = 'active'", (key,)).fetchone()
    cur = conn.execute("INSERT INTO samples (job_id, plugin, playbook, input, output, tier, escalated, created_at,"
                       " lessons_id, subject, used) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (job_id, plugin, key, _j(input), _j(output), tier, int(escalated), now,
                        lessons[0] if lessons else None, (subject or "")[:2000] or None,
                        json.dumps([int(u) for u in used]) if used else None))
    # keep the newest `keep` without a verdict; marked ones are the eval set and stay; confirmed ones (left alone
    # for a day) are the worked examples, the newest EXAMPLES_KEPT of them
    conn.execute("DELETE FROM samples WHERE playbook = ? AND verdict IS NULL AND kept_at IS NULL AND id NOT IN"
                 " (SELECT id FROM samples WHERE playbook = ? AND verdict IS NULL AND kept_at IS NULL"
                 " ORDER BY id DESC LIMIT ?)", (key, key, keep))
    conn.execute("DELETE FROM samples WHERE playbook = ? AND verdict IS NULL AND kept_at IS NOT NULL AND id NOT IN"
                 " (SELECT id FROM samples WHERE playbook = ? AND verdict IS NULL AND kept_at IS NOT NULL"
                 " ORDER BY id DESC LIMIT ?)", (key, key, EXAMPLES_KEPT))
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
    keys = r.keys()
    for k in ("subject", "feedback", "kept_at"):
        if k in keys and r[k] is not None:
            out[k] = r[k]
    if "used" in keys and r["used"]:
        out["examples_used"] = _p(r["used"])
    lid = r["lessons_id"] if "lessons_id" in keys else None
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


def set_verdict(conn: sqlite3.Connection, sample_id: int, verdict: str | None, correction: Any = None,
                feedback: str = "you") -> bool:
    old = conn.execute("SELECT verdict, used FROM samples WHERE id = ?", (sample_id,)).fetchone()
    if old is None:
        return False
    conn.execute("UPDATE samples SET verdict = ?, correction = ?, feedback = ?,"
                 " reviewed_at = CASE WHEN ? = 'wrong' THEN NULL ELSE reviewed_at END WHERE id = ?",
                 (verdict, _j(correction) if correction not in (None, "") else None, feedback if verdict else None,
                  verdict, sample_id))
    if verdict == "wrong" and old["verdict"] != "wrong" and old["used"]:  # the examples it was shown misled it
        ids = [int(i) for i in _p(old["used"]) or []]
        if ids:
            conn.execute(f"UPDATE samples SET harm = harm + 1 WHERE id IN ({','.join('?' * len(ids))})", ids)
    return True


def _names(subject: str | None) -> set[str]:
    return {os.path.basename(x.strip()).lower() for x in (subject or "").splitlines() if x.strip()}


def implicit(conn: sqlite3.Connection, job_id: str, subject: str | None, verdict: str, correction: Any,
             feedback: str) -> list[int]:
    """Your fix counts as a verdict without a click: an undo, the Wrong button, a file you moved back or renamed
    again, "no, I meant ..." to Ari. Marks that job's answer about `subject` (a file name; None: all its answers).
    Never overrides a verdict you gave yourself. Returns the sample ids marked."""
    rows = conn.execute("SELECT id, subject, verdict, feedback FROM samples WHERE job_id = ?", (job_id,)).fetchall()
    want = _names(subject)
    hit = [r for r in rows if not want or want & _names(r["subject"])]
    if not hit and want and len(rows) == 1 and not rows[0]["subject"]:
        hit = list(rows)  # one answer, about nothing named: it was this one
    out = []
    for r in hit:
        if r["verdict"] and r["feedback"] in (None, "you"):
            continue  # yours stands
        set_verdict(conn, r["id"], verdict, correction, feedback)
        conn.execute("UPDATE samples SET kept_at = NULL WHERE id = ?", (r["id"],))
        out.append(r["id"])
    return out


def examples(conn: sqlite3.Connection, plugin: str) -> dict[str, list[dict[str, Any]]]:
    """Experience memory, sent to workers with each job: per playbook, the answers you confirmed (Correct, or a
    fix that says what it should have been) and ones left alone for a day. Ones that kept misleading are left out."""
    out: dict[str, list[dict[str, Any]]] = {}
    for r in conn.execute(
            "SELECT s.id, s.playbook, s.input, s.output, s.verdict, s.correction FROM samples s"
            " JOIN playbooks p ON p.key = s.playbook WHERE p.plugin = ? AND ("
            " (s.verdict = 'correct' AND s.harm < ?) OR (s.verdict = 'wrong' AND s.correction IS NOT NULL AND"
            " s.harm < ?) OR (s.verdict IS NULL AND s.kept_at IS NOT NULL AND s.harm < ?))"
            " ORDER BY s.id DESC", (plugin, HARM_LIMIT + 1, HARM_LIMIT + 1, HARM_LIMIT)).fetchall():
        lst = out.setdefault(r["playbook"], [])
        if len(lst) >= EXAMPLES_SENT or len(r["input"]) > 3000:
            continue
        fixed = r["verdict"] == "wrong" or (r["verdict"] == "correct" and r["correction"])
        lst.append({"id": r["id"], "input": _p(r["input"]),
                    "answer": _p(r["correction"]) if fixed else _p(r["output"]), "fixed": bool(fixed)})
    return out


def followups(conn: sqlite3.Connection, now: float, days: float = 3) -> dict[str, list[dict[str, Any]]]:
    """Moves made by jobs whose answers are still open (no verdict, not kept yet): per plugin, for a worker to look
    whether you moved the file back, renamed it again, moved it elsewhere, or left it."""
    since = now - days * 86400
    out: dict[str, list[dict[str, Any]]] = {}
    jobs = conn.execute("SELECT DISTINCT s.job_id, s.plugin FROM samples s WHERE s.created_at >= ? AND"
                        " s.verdict IS NULL AND s.kept_at IS NULL AND s.subject IS NOT NULL", (since,)).fetchall()
    for j in jobs:
        undone = {r[0] for r in conn.execute(  # Undo / Wrong pressed: that already said it
            "SELECT dedupe_key FROM jobs WHERE dedupe_key IN (SELECT 'undo:' || id FROM events WHERE job_id = ?"
            " UNION SELECT 'fix:' || id FROM events WHERE job_id = ?)", (j["job_id"], j["job_id"]))}
        for e in conn.execute("SELECT id, data, at FROM events WHERE job_id = ? AND kind = 'file.moved'",
                              (j["job_id"],)):
            d = _p(e["data"]) or {}
            if d.get("dry_run") or f"undo:{e['id']}" in undone or f"fix:{e['id']}" in undone:
                continue
            out.setdefault(j["plugin"], []).append({"job_id": j["job_id"], "event_id": e["id"], "from": d.get("from"),
                                                    "to": d.get("to"), "size": d.get("size"),
                                                    "mtime": d.get("mtime"), "age": now - e["at"]})
    return out


FOLLOWUP_WORDS = {"moved back": "you moved it back to where it was", "renamed": "you renamed it to {name}",
                  "moved": "you moved it to {folder}"}


def record_followups(conn: sqlite3.Connection, now: float, results: list[dict[str, Any]]) -> dict[str, int]:
    """Apply what the worker saw: a fix of yours is a wrong answer (with what you did); a change left alone for a
    day is a yes (the answer becomes a worked example)."""
    counts = {"wrong": 0, "kept": 0}
    for x in results:
        job, status = str(x.get("job_id") or ""), str(x.get("status") or "")
        subject = str(x.get("from") or "")
        if status in FOLLOWUP_WORDS:
            at = str(x.get("now_at") or "")
            words = FOLLOWUP_WORDS[status].format(name=os.path.basename(at), folder=os.path.dirname(at))
            counts["wrong"] += len(implicit(conn, job, subject, "wrong", words, status))
        elif status == "there" and float(x.get("age") or 0) >= KEEP_AFTER:
            want = _names(subject)
            for r in conn.execute("SELECT id, subject FROM samples WHERE job_id = ? AND verdict IS NULL AND"
                                  " kept_at IS NULL", (job,)).fetchall():
                if not want or want & _names(r["subject"]):
                    conn.execute("UPDATE samples SET kept_at = ? WHERE id = ?", (now, r["id"]))
                    counts["kept"] += 1
    return counts


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
