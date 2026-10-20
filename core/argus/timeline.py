"""What Ari did, as stories you can follow, and what isn't working.

- **Requests** (`requests`): one row per thing you asked Ari: what was heard (and how sure the language check was),
  what Ari did (the tools, the models that answered or were rejected), the answer, how long it took, and the result:
  ok, failed (and why), stopped, or ignored (speech that wasn't for Ari, and why). Helios > Ari > Timeline, the
  console's Logs pane and `dev.ps1 trace` show them.
- **A trace** (`trace`): one request in full, in order: heard → asked → each step → each model call → each tool →
  the answer → spoken.
- **Problems** (`problems`): what went wrong in the last hours, grouped by cause with counts and an example, so a
  repeated failure reads as one line ("Spotify search box not found ×4"), not forty log lines.

Everything is read from Argus's own tables (ari_turns, jobs, steps, model_calls, events); nothing new is stored
except the listener's notes on a request (`ari_turns.meta`: how it was heard, when it was spoken) and its
`ari.ignored` events.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from typing import Any

FAILED_JOB = ("dead", "cancelled")
NUMS = re.compile(r"\b[0-9a-f]{8,}\b|\d+(?:\.\d+)?")


def _json(v: Any) -> Any:
    if not v:
        return None
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return None


def first_line(text: Any, n: int = 140) -> str:
    s = str(text or "").strip().splitlines()
    out = s[0] if s else ""
    for line in reversed(s):  # a traceback: its last line says what broke
        if re.match(r"^\w+(?:Error|Exception|Exhausted|Lost|Failed)\b", line.strip()):
            out = line.strip()
            break
    return out[:n]


def _calls(conn: sqlite3.Connection, job_id: str) -> list[dict[str, Any]]:
    try:
        rows = conn.execute("SELECT model, tier, verdict, reason, latency_ms FROM model_calls WHERE job_id = ?"
                            " ORDER BY id", (job_id,)).fetchall()
    except sqlite3.OperationalError:  # an older database without the call log
        return []
    return [{"model": r["model"] or r["tier"], "verdict": r["verdict"], "ms": r["latency_ms"],
             "why": first_line(r["reason"], 100) if r["reason"] else None} for r in rows]


def _tools(result: dict | None) -> list[dict[str, Any]]:
    out = []
    for u in (result or {}).get("used") or []:
        if not isinstance(u, dict):
            continue
        res = u.get("result")
        problem = None
        if "error" in u:
            problem = first_line(u["error"])
        elif isinstance(res, dict) and res.get("done") is False:
            problem = first_line(res.get("problem") or "not done")
        out.append({"tool": u.get("tool"), "ok": problem is None, "why": problem,
                    "private": bool(u.get("private"))})
    return out


def story(conn: sqlite3.Connection, you: sqlite3.Row, ari: sqlite3.Row | None) -> dict[str, Any]:
    """One request: what you said and everything that followed."""
    meta = _json(you["meta"]) if "meta" in you.keys() else None
    meta = meta if isinstance(meta, dict) else {}
    out: dict[str, Any] = {"at": you["created_at"], "conv": you["conv"], "turn": you["id"], "text": you["text"],
                           "source": meta.get("source") or "typed", "heard": meta.get("heard") or None,
                           "reply": None, "job_id": None, "result": "waiting", "why": None, "ms": None,
                           "tools": [], "models": [], "answer_ms": None, "spoken": None}
    if ari is None:
        return out
    ameta = _json(ari["meta"]) if "meta" in ari.keys() else None
    if isinstance(ameta, dict):
        out["answer_ms"] = ameta.get("answer_ms")  # from when you finished speaking to the answer at the PC
        out["spoken"] = ameta.get("spoken")
    out["reply"] = ari["text"]
    out["job_id"] = ari["job_id"]
    if not ari["job_id"]:  # answered at once (the rules), or a yes / no
        out.update(result="ok", via="rules", ms=round((ari["created_at"] - you["created_at"]) * 1000))
        return out
    j = conn.execute("SELECT state, result, error, created_at, finished_at FROM jobs WHERE id = ?",
                     (ari["job_id"],)).fetchone()
    if j is None:
        out.update(result="failed", why="its job is gone (pruned)")
        return out
    res = _json(j["result"])
    out["tools"] = _tools(res if isinstance(res, dict) else None)
    out["models"] = _calls(conn, ari["job_id"])
    if j["finished_at"]:
        out["ms"] = round((j["finished_at"] - you["created_at"]) * 1000)
    if j["state"] == "succeeded":
        bad = [t for t in out["tools"] if not t["ok"]]
        out["result"] = "failed" if bad else "ok"
        if bad:
            out["why"] = bad[0]["why"]  # the tool's name is in `tools`
    elif j["state"] == "cancelled":
        out.update(result="stopped", why=first_line(j["error"]) or "stopped")
    elif j["state"] == "dead":
        out.update(result="failed", why=first_line(j["error"]) or "the job failed")
    else:
        out["result"] = "working"
    return out


def requests(conn: sqlite3.Connection, *, limit: int = 50, before: float | None = None,
             ignored: bool = True) -> list[dict[str, Any]]:
    """The newest requests first, with the ignored speech in between (unless `ignored` is False)."""
    rows = conn.execute("SELECT * FROM ari_turns WHERE role = 'you'" + (" AND created_at < ?" if before else "")
                        + " ORDER BY id DESC LIMIT ?", ((before, limit) if before else (limit,))).fetchall()
    out = []
    for you in rows:
        ari = conn.execute("SELECT * FROM ari_turns WHERE conv = ? AND role = 'ari' AND id > ? ORDER BY id LIMIT 1",
                           (you["conv"], you["id"])).fetchone()
        out.append(story(conn, you, ari))
    if ignored and out:
        t0 = out[-1]["at"] if len(out) >= limit else 0.0
        t1 = before or time.time() + 1
        for e in conn.execute("SELECT at, data FROM events WHERE kind = 'ari.ignored' AND at >= ? AND at < ?"
                              " ORDER BY rowid DESC LIMIT 200", (t0, t1)):
            d = _json(e["data"]) or {}
            out.append({"at": e["at"], "result": "ignored", "text": d.get("text"), "why": d.get("why"),
                        "source": "voice", "heard": {k: d.get(k) for k in ("lang", "top_p", "english") if k in d}})
        out.sort(key=lambda r: r["at"], reverse=True)
    elif ignored:  # no requests at all: still show what was ignored
        for e in conn.execute("SELECT at, data FROM events WHERE kind = 'ari.ignored' ORDER BY rowid DESC LIMIT ?",
                              (limit,)):
            d = _json(e["data"]) or {}
            out.append({"at": e["at"], "result": "ignored", "text": d.get("text"), "why": d.get("why"),
                        "source": "voice"})
    return out


def line(r: dict[str, Any]) -> str:
    """A request as one plain line: `14:02 ✗ "play famous" → play_on_spotify: search box not found · 8.2 s`."""
    mark = {"ok": "✓", "failed": "✗", "stopped": "■", "ignored": "·", "working": "…", "waiting": "…"}.get(
        r.get("result") or "", "?")
    t = time.strftime("%H:%M", time.localtime(r.get("at") or 0))
    s = f"{t} {mark} \"{(r.get('text') or '')[:60]}\""
    tools = [x["tool"] for x in r.get("tools") or []]
    if tools:
        s += " → " + ", ".join(tools[:3])
    if r.get("why"):
        s += f": {r['why']}" if tools else f" — {r['why']}"
    elif r.get("reply"):
        s += f" → \"{str(r['reply'])[:60]}\""
    if r.get("ms"):
        s += f" · {r['ms'] / 1000:.1f} s"
    return s


def trace(conn: sqlite3.Connection, ref: str) -> dict[str, Any] | None:
    """One request in full, by its job id, its turn id, or "last"."""
    if ref == "last":
        you = conn.execute("SELECT * FROM ari_turns WHERE role = 'you' ORDER BY id DESC LIMIT 1").fetchone()
    elif ref.isdigit():
        you = conn.execute("SELECT * FROM ari_turns WHERE id = ? AND role = 'you'", (int(ref),)).fetchone()
    else:
        ari = conn.execute("SELECT * FROM ari_turns WHERE job_id = ? AND role = 'ari'", (ref,)).fetchone()
        you = conn.execute("SELECT * FROM ari_turns WHERE conv = ? AND role = 'you' AND id < ? ORDER BY id DESC"
                           " LIMIT 1", (ari["conv"], ari["id"])).fetchone() if ari is not None else None
        if you is None:  # any other job: its steps and calls only
            return job_trace(conn, ref)
    if you is None:
        return None
    ari = conn.execute("SELECT * FROM ari_turns WHERE conv = ? AND role = 'ari' AND id > ? ORDER BY id LIMIT 1",
                       (you["conv"], you["id"])).fetchone()
    s = story(conn, you, ari)
    s["steps"] = job_trace(conn, s["job_id"])["steps"] if s.get("job_id") else []
    return s


def job_trace(conn: sqlite3.Connection, job_id: str) -> dict[str, Any] | None:
    j = conn.execute("SELECT id, plugin, workflow, state, error, created_at, finished_at, result FROM jobs"
                     " WHERE id = ?", (job_id,)).fetchone()
    if j is None:
        return None
    steps: list[dict[str, Any]] = []
    for s in conn.execute("SELECT name, state, tier_used, error, started_at, finished_at FROM steps WHERE job_id = ?"
                          " ORDER BY idx", (job_id,)):
        ms = round((s["finished_at"] - s["started_at"]) * 1000) if s["finished_at"] and s["started_at"] else None
        steps.append({"at": s["started_at"], "kind": "step", "what": s["name"], "ok": s["state"] != "failed",
                      "ms": ms, "why": first_line(s["error"]) if s["error"] else None, "tier": s["tier_used"]})
    try:
        for c in conn.execute("SELECT created_at, model, tier, verdict, reason, latency_ms, step FROM model_calls"
                              " WHERE job_id = ? ORDER BY id", (job_id,)):
            steps.append({"at": c["created_at"], "kind": "model", "what": f"{c['model'] or c['tier']} ({c['step']})"
                          if c["step"] else str(c["model"] or c["tier"]), "ok": c["verdict"] == "ok",
                          "ms": c["latency_ms"], "why": first_line(c["reason"], 160) if c["reason"] else None,
                          "verdict": c["verdict"]})
    except sqlite3.OperationalError:
        pass
    steps.sort(key=lambda x: x["at"] or 0)
    return {"job_id": j["id"], "plugin": j["plugin"], "workflow": j["workflow"], "result": j["state"],
            "why": first_line(j["error"]) if j["error"] else None, "at": j["created_at"],
            "ms": round((j["finished_at"] - j["created_at"]) * 1000) if j["finished_at"] else None, "steps": steps}


def _key(text: str) -> str:
    """Errors that differ only in ids, numbers or times are the same problem."""
    return NUMS.sub("#", text.lower())[:90]


HINTS = {
    "unreachable": "the PC couldn't reach the laptop (Tailscale down or still starting after a boot)",
    "getaddrinfo": "the laptop's name didn't resolve (Tailscale still starting after a boot)",
    "not one of the tools": "the local model picked a tool that doesn't exist (lessons or a better model help)",
    "another language": "speech that wasn't English was ignored (ari.languages)",
    "no wake phrase": "\"Hey Ari\" wasn't heard at the start",
    "timed out": "something took too long (a model loading, or the PC busy)",
    "token": "a sign-in or key is missing or expired",
}


def hint(text: str) -> str | None:
    low = text.lower()
    return next((h for k, h in HINTS.items() if k in low), None)


def problems(conn: sqlite3.Connection, now: float, hours: float = 24) -> dict[str, Any]:
    """What went wrong since `hours` ago, grouped by cause: [{area, what, count, last_at, example, hint}]."""
    since = now - hours * 3600
    groups: dict[tuple[str, str], dict[str, Any]] = {}

    def add(area: str, what: str, at: float, example: dict | None = None) -> None:
        k = (area, _key(what))
        g = groups.setdefault(k, {"area": area, "what": what, "count": 0, "last_at": 0.0, "example": example,
                                  "hint": hint(what)})
        g["count"] += 1
        if at >= g["last_at"]:
            g["last_at"], g["what"] = at, what
            if example:
                g["example"] = example

    for j in conn.execute("SELECT id, plugin, workflow, state, error, finished_at, result FROM jobs"
                          " WHERE finished_at >= ? AND state IN ('dead', 'succeeded')", (since,)):
        if j["state"] == "dead":
            add(f"{j['plugin']}.{j['workflow']}", first_line(j["error"]) or "failed", j["finished_at"],
                {"job_id": j["id"]})
            continue
        res = _json(j["result"])
        for t in _tools(res if isinstance(res, dict) else None):
            if not t["ok"]:
                add(f"tool {t['tool']}", t["why"] or "failed", j["finished_at"], {"job_id": j["id"]})
    try:
        for c in conn.execute("SELECT job_id, model, tier, verdict, reason, created_at FROM model_calls"
                              " WHERE created_at >= ? AND verdict != 'ok'", (since,)):
            add(f"model {c['model'] or c['tier']}", f"{c['verdict']}: {first_line(c['reason'], 100)}",
                c["created_at"], {"job_id": c["job_id"]})
    except sqlite3.OperationalError:
        pass
    for e in conn.execute("SELECT at, data FROM events WHERE kind = 'ari.ignored' AND at >= ?", (since,)):
        d = _json(e["data"]) or {}
        why = str(d.get("why") or "ignored")
        add("hearing", re.sub(r"\s*\(.*\)", "", why), e["at"], {"text": d.get("text")})
    for e in conn.execute("SELECT at, from_component AS src FROM events WHERE kind = 'worker.offline'"
                          " AND at >= ?", (since,)):
        add("machines", f"{e['src']} went silent", e["at"])
    items = sorted(groups.values(), key=lambda g: (-g["count"], -g["last_at"]))
    return {"since": since, "hours": hours, "count": sum(g["count"] for g in items), "problems": items}


def headline(p: dict[str, Any], n: int = 2) -> str:
    """"3 problems today: tool play_on_spotify (×2), hearing (×1)" or "nothing failed"."""
    items = p.get("problems") or []
    if not items:
        return "nothing failed"
    parts = [f"{g['area']}" + (f" ×{g['count']}" if g["count"] > 1 else "") for g in items[:n]]
    more = f" +{len(items) - n} more" if len(items) > n else ""
    return f"{p['count']} problem{'s' if p['count'] != 1 else ''}: " + ", ".join(parts) + more


def lanes(conn: sqlite3.Connection, now: float, minutes: float = 60) -> dict[str, Any]:
    """What each machine worked on lately: [{worker, jobs: [{start, end, plugin, workflow, state}]}], from the
    job events (a worker takes a job: job.leased; it ends: succeeded, dead, cancelled, retry, waiting)."""
    since = now - minutes * 60
    open_: dict[str, dict[str, Any]] = {}
    out: dict[str, list[dict[str, Any]]] = {}
    ends = ("job.succeeded", "job.dead", "job.cancelled", "job.retry", "job.waiting")
    for e in conn.execute("SELECT job_id, kind, from_component, to_component, at FROM events WHERE at >= ? AND"
                          " kind IN ('job.leased', 'job.succeeded', 'job.dead', 'job.cancelled', 'job.retry',"
                          " 'job.waiting') ORDER BY at", (since - 600,)):
        if e["kind"] == "job.leased":
            open_[e["job_id"]] = {"worker": e["from_component"] or "?", "start": e["at"], "end": None,
                                  "plugin": e["to_component"], "job_id": e["job_id"]}
        elif e["kind"] in ends and e["job_id"] in open_:
            j = open_.pop(e["job_id"])
            j["end"], j["state"] = e["at"], e["kind"].split(".", 1)[1]
            if j["end"] >= since:
                out.setdefault(j["worker"], []).append(j)
    for j in open_.values():  # still running
        j["state"] = "running"
        out.setdefault(j["worker"], []).append(j)
    ids = [j["job_id"] for v in out.values() for j in v]
    names: dict[str, str] = {}
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        q = "SELECT id, workflow FROM jobs WHERE id IN (" + ",".join("?" * len(part)) + ")"
        names.update({r["id"]: r["workflow"] for r in conn.execute(q, part)})
    for v in out.values():
        for j in v:
            j["workflow"] = names.get(j["job_id"])
    return {"since": since, "now": now, "workers": [{"worker": w, "jobs": v} for w, v in sorted(out.items())]}


def activity(conn: sqlite3.Connection, now: float, hours: int = 24) -> dict[str, Any]:
    """The last `hours` hour by hour, for the console's charts: jobs finished (ok / failed) and what you asked Ari
    (ok / failed / ignored), plus today's totals and the average answer time. The last bucket is the current hour."""
    hours = max(1, min(int(hours), 24 * 7))
    start = (int(now) // 3600 - hours + 1) * 3600
    jobs = [{"ok": 0, "failed": 0} for _ in range(hours)]
    ari = [{"ok": 0, "failed": 0, "ignored": 0} for _ in range(hours)]

    def at(t: float) -> int | None:
        i = int((t - start) // 3600)
        return i if 0 <= i < hours else None

    for r in conn.execute("SELECT state, finished_at FROM jobs WHERE finished_at >= ?", (start,)):
        i = at(r["finished_at"])
        if i is not None and r["state"] in ("succeeded", *FAILED_JOB):
            jobs[i]["ok" if r["state"] == "succeeded" else "failed"] += 1
    ms: list[float] = []
    for s in requests(conn, limit=400, before=None, ignored=True):
        if s["at"] < start:
            continue
        i = at(s["at"])
        res = s.get("result")
        key = "ok" if res == "ok" else "ignored" if res == "ignored" else "failed" if res in ("failed", "stopped") \
            else None
        if i is not None and key:
            ari[i][key] += 1
        took = s.get("answer_ms") if s.get("answer_ms") is not None else s.get("ms")
        if res == "ok" and took is not None:
            ms.append(float(took))
    total = {k: sum(b[k] for b in ari) for k in ("ok", "failed", "ignored")}
    total.update(jobs_ok=sum(b["ok"] for b in jobs), jobs_failed=sum(b["failed"] for b in jobs),
                 answer_ms=round(sum(ms) / len(ms)) if ms else None)
    return {"start": start, "now": now, "hours": hours, "jobs": jobs, "ari": ari, "total": total}
