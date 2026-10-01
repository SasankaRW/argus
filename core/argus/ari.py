"""Ari: Argus as a personal assistant you talk to (Helios, the phone, "Hey Ari" on the PC).

A conversation is a list of turns (table `ari_turns`). Each message you send goes through, in order:

1. An answer to a question Ari asked ("Shall I?"): yes does it, no drops it.
2. A time ("every morning at 7", "tomorrow at 5 pm", "in 20 minutes") plus something to do ("sort downloads",
   "shut down the PC", "remind me to call mum"): Ari says what it understood and asks you to confirm; then it
   becomes a schedule of yours (Helios > Schedules), run by the scheduler.
3. The common asks (rules in `argus.ask`): answered at once; a suggested action waits for your yes.
4. Anything else: a model answers as Ari, with the last few turns as context (a job; the turn fills in when it
   finishes).

Nothing runs without your yes (or a tap on Do it).
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .cron import next_run
from .ids import new_id

YES = re.compile(r"^\s*(yes|yeah|yep|yup|sure|ok|okay|do it|go ahead|please do|confirm|correct|right|sounds good)\b",
                 re.I)
NO = re.compile(r"^\s*(no|nope|nah|cancel|don'?t|do not|never ?mind|stop|forget it)\b", re.I)
WAKE = re.compile(r"^\s*(hey|hi|ok|okay)?[\s,]*(ari|arie|harry|artie|argus)\b[\s,.!?]*", re.I)
DAYS = ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"]
PARTS = {"morning": 8, "afternoon": 14, "evening": 19, "night": 22, "tonight": 20}
TIME = r"(?:(?P<h>\d{1,2})(?:[:.](?P<m>\d{2}))?\s*(?P<ap>a\.?\s?m\.?|p\.?\s?m\.?)?|(?P<word>noon|midday|midnight))"
TIME_AP = (r"\b(?:(?P<h>\d{1,2})(?:[:.](?P<m>\d{2}))?\s*(?P<ap>a\.?\s?m\.?|p\.?\s?m\.?)|(?P<word>noon|midday|midnight))"
           r"(?=\s)")
HISTORY = 8  # turns the model sees
HISTORY_S = 30 * 60  # ... from the last half hour only: an old chat doesn't steer a new question


@dataclass
class When:
    cron: str
    once: bool
    say: str  # "every day at 07:00", "tomorrow at 17:00"
    rest: str  # the text without the time words: what to do


def clock(h: int, m: int) -> str:
    """7 am, 7:30 pm, noon, midnight: how people say it (and how it reads aloud)."""
    if (h, m) == (12, 0):
        return "noon"
    if (h, m) == (0, 0):
        return "midnight"
    ap = "am" if h < 12 else "pm"
    h12 = h % 12 or 12
    return f"{h12} {ap}" if m == 0 else f"{h12}:{m:02d} {ap}"


def _hm(m: re.Match, part: str | None) -> tuple[int, int] | None:
    if m.group("word"):
        return (12, 0) if m.group("word") in ("noon", "midday") else (0, 0)
    h, mi = int(m.group("h")), int(m.group("m") or 0)
    ap = (m.group("ap") or "").replace(".", "").replace(" ", "").lower()
    if h > 23 or mi > 59 or (ap and not 1 <= h <= 12):
        return None
    if ap == "pm" and h < 12:
        h += 12
    elif ap == "am" and h == 12:
        h = 0
    elif not ap and h < 12 and part in ("afternoon", "evening", "night", "tonight"):
        h += 12
    return h, mi


def parse_when(text: str, now: float) -> When | None:
    """Find a time in plain words. None when there is none."""
    t = " " + text.lower().strip() + " "
    t = re.sub(r"[?!,]", " ", t)
    at = re.search(rf"\b(?:at|@|by)\s*{TIME}(?=\s)", t) or re.search(TIME_AP, t)
    part_m = re.search(r"\b(morning|afternoon|evening|night|tonight)s?\b", t)
    part = part_m.group(1) if part_m else None
    used: list[str] = []

    def cut(s: str) -> None:
        used.append(s)

    hm = None
    if at:
        hm = _hm(at, part)
        if hm is None:
            return None
        cut(at.group(0))
    now_dt = datetime.fromtimestamp(now)

    # every N minutes / hours; every hour
    m = re.search(r"\bevery\s+(\d+)\s*(minutes?|mins?|hours?|hrs?)\b", t)
    if m:
        n = int(m.group(1))
        if m.group(2).startswith("m") and 1 <= n <= 59:
            cut(m.group(0))
            return When(f"*/{n} * * * *", False, f"every {n} minutes", _rest(t, used))
        if m.group(2).startswith("h") and 1 <= n <= 23:
            cut(m.group(0))
            return When(f"0 */{n} * * *", False, f"every {n} hours", _rest(t, used))
        return None
    m = re.search(r"\b(every hour|hourly|each hour)\b", t)
    if m:
        cut(m.group(0))
        return When("0 * * * *", False, "every hour", _rest(t, used))

    # in N minutes / hours (once)
    m = re.search(r"\bin\s+(\d+|an?|one|half an?)\s*(minutes?|mins?|hours?|hrs?)\b", t)
    if m:
        q = {"a": 1, "an": 1, "one": 1}.get(m.group(1), None)
        if m.group(1).startswith("half"):
            mins = 30
        else:
            n = q if q is not None else int(m.group(1))
            mins = n if m.group(2).startswith("m") else n * 60
        if not 1 <= mins <= 7 * 24 * 60:
            return None
        cut(m.group(0))
        at_dt = now_dt + timedelta(minutes=mins)
        return When(f"{at_dt.minute} {at_dt.hour} {at_dt.day} {at_dt.month} *", True,
                    f"at {clock(at_dt.hour, at_dt.minute)}" + ("" if at_dt.date() == now_dt.date() else " tomorrow"),
                    _rest(t, used))

    # repeating days
    days = None
    m = re.search(r"\b(every ?day|daily|each day|every (morning|afternoon|evening|night)|each (morning|evening|night)"
                  r"|(?:in the )?(mornings|evenings|nights))\b", t)
    if m:
        days, say_days = "*", "every day"
        cut(m.group(0))
    m = re.search(r"\b(every weekday|on weekdays|weekdays|every work ?day)\b", t)
    if m:
        days, say_days = "1-5", "every weekday"
        cut(m.group(0))
    m = re.search(r"\b(every weekend|on weekends|weekends)\b", t)
    if m:
        days, say_days = "0,6", "every weekend"
        cut(m.group(0))
    named = [i for i, d in enumerate(DAYS) if re.search(rf"\b(every |on )?{d}s?\b", t)]
    if named and days is None:
        days = ",".join(str(i) for i in named)
        say_days = "every " + " and ".join(DAYS[i].capitalize() for i in named)
        for d in (DAYS[i] for i in named):
            cut(re.search(rf"\b(every |on )?{d}s?\b", t).group(0))  # type: ignore[union-attr]
    if days is not None:
        if hm is None:
            hm = (PARTS.get(part or "", 9), 0)
        if part_m:
            cut(part_m.group(0))
        return When(f"{hm[1]} {hm[0]} * * {days}", False, f"{say_days} at {clock(*hm)}", _rest(t, used))

    # once: today / tonight / tomorrow at ..., or just "at 5 pm"
    m = re.search(r"\b(tomorrow|today|tonight|this (morning|afternoon|evening))\b", t)
    if hm is None and not (m and part):
        return None
    if hm is None:
        hm = (PARTS[part], 0)  # type: ignore[index]
    day = now_dt.date()
    if m:
        cut(m.group(0))
        if m.group(1) == "tomorrow":
            day += timedelta(days=1)
    if part_m:
        cut(part_m.group(0))
    target = datetime(day.year, day.month, day.day, hm[0], hm[1])
    if target <= now_dt and not (m and m.group(1) == "tomorrow"):
        if at and not at.group("ap") and hm[0] < 12 and target.replace(hour=hm[0] + 12) > now_dt:
            target = target.replace(hour=hm[0] + 12)  # "at 5" said at 14:00 means 17:00
        else:
            target += timedelta(days=1)
    when = "today" if target.date() == now_dt.date() else "tomorrow" if target.date() == now_dt.date() + \
        timedelta(days=1) else f"on {target:%a %d %b}"
    return When(f"{target.minute} {target.hour} {target.day} {target.month} *", True,
                f"{when} at {clock(target.hour, target.minute)}", _rest(t, used))


def _rest(t: str, used: list[str]) -> str:
    for u in used:
        t = t.replace(u, " ", 1)
    t = re.sub(r"\b(please|can you|could you|would you|will you|i want you to|and|then|also)\b", " ", t)
    return re.sub(r"\s+", " ", t).strip(" .")


REMIND = re.compile(r"^(?:remind me|tell me|ping me|notify me)\s*(?:to|about|that|of)?\s*(?P<what>.*)$", re.I)


def action_for(rest: str, actions: list[dict[str, Any]]) -> tuple[str, dict[str, Any]] | None:
    """What to do at that time: (action id, extra input), from the text left after the time words."""
    from .ask import POWER, POWER_WORDS, rules

    m = REMIND.match(rest)
    if m:
        what = m.group("what").strip() or "your reminder"
        return f"remind:{what[:300]}", {}
    for a, rx in POWER_WORDS.items():
        if a in ("sleep", "shutdown", "restart", "wake") and re.search(rx, rest) and \
                re.search(r"\b(pc|computer|machine|desktop)\b", rest):
            return f"power:{a}", {"label": POWER[a]}
    hit = rules(rest, actions, {"queue": {"running": [], "queued": []}, "approvals": [], "recent": []})
    if hit and str(hit.get("action") or "").startswith("run:"):
        return hit["action"], {}
    return None


def label_of(action: str, actions: list[dict[str, Any]]) -> str:
    if action.startswith("remind:"):
        return f'remind you: "{action[7:]}"'
    lab = next((a["label"] for a in actions if a["id"] == action), action)
    lab = re.sub(r"\s+now$", "", lab, flags=re.I)  # "Sort Downloads now" -> "sort Downloads" at a later time
    return lab[0].lower() + lab[1:] if lab else action


# ------------------------------------------------------------------ storage

def add_turn(conn: sqlite3.Connection, conv: str, role: str, text: str | None, *, action: str | None = None,
             pending: dict | None = None, job_id: str | None = None) -> int:
    cur = conn.execute("INSERT INTO ari_turns (conv, role, text, action, pending, job_id, created_at)"
                       " VALUES (?,?,?,?,?,?,?)",
                       (conv, role, text, action, json.dumps(pending) if pending else None, job_id, time.time()))
    return int(cur.lastrowid or 0)


def turns(conn: sqlite3.Connection, conv: str, limit: int = 200) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM (SELECT * FROM ari_turns WHERE conv = ? ORDER BY id DESC LIMIT ?)"
                        " ORDER BY id", (conv, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["pending"] = json.loads(d["pending"]) if d["pending"] else None
        d["used"] = json.loads(d["used"]) if d.get("used") else None
        out.append(d)
    return out


def open_question(conn: sqlite3.Connection, conv: str, max_age: float | None = None) -> dict[str, Any] | None:
    """The last thing Ari asked you to confirm, if it is still open (the newest Ari turn has it) and, with `max_age`,
    asked within that many seconds: a "yes" an hour later is about something new, not that old question."""
    r = conn.execute("SELECT id, pending, created_at FROM ari_turns WHERE conv = ? AND role = 'ari'"
                     " ORDER BY id DESC LIMIT 1", (conv,)).fetchone()
    if r and r["pending"] and (max_age is None or time.time() - float(r["created_at"] or 0) <= max_age):
        return {"turn": r["id"], **json.loads(r["pending"])}
    return None


def close_question(conn: sqlite3.Connection, turn_id: int) -> None:
    conn.execute("UPDATE ari_turns SET pending = NULL WHERE id = ?", (turn_id,))


def history(conn: sqlite3.Connection, conv: str) -> list[dict[str, Any]]:
    """The chat so far for the model. Ari's last answer also carries what its tools found (clipped), so a
    follow-up like "open it" or "and the other one?" knows what "it" was."""
    recent = time.time() - HISTORY_S
    out: list[dict[str, Any]] = [{"role": t["role"], "text": t["text"], "job_id": t.get("job_id")}
                                 for t in turns(conn, conv, HISTORY)
                                 if t["text"] and float(t.get("created_at") or 0) >= recent]
    last = next((t for t in reversed(out) if t["role"] == "ari" and t["job_id"]), None)
    for t in out:
        if t["role"] != "ari" or not t["job_id"]:
            continue
        row = conn.execute("SELECT result FROM jobs WHERE id = ?", (t["job_id"],)).fetchone()
        try:
            used = (json.loads(row[0]) or {}).get("used") if row and row[0] else None
        except (ValueError, AttributeError):
            used = None
        if not isinstance(used, list):
            continue
        if any(isinstance(u, dict) and u.get("private") for u in used):
            t["private"] = True  # the screen or clipboard was in this chat: Ari's thinking stays local
        public = [u for u in used if isinstance(u, dict) and not u.get("private")]
        if t is last and public:
            found = [{k: u[k] for k in ("tool", "args", "result", "error") if k in u} for u in public[-3:]]
            s = json.dumps(found, ensure_ascii=False, default=str)
            t["found"] = found if len(s) <= 1200 else s[:1200] + "…"
    for t in out:
        t.pop("job_id")
    return out


def add_schedule(conn: sqlite3.Connection, now: float, when: When, action: str, *, plugin: str, workflow: str,
                 input: dict, needs: list[str], priority: int, label: str) -> dict[str, Any]:
    sid = "you-" + new_id().lower()[:12]
    spec = {"input": input, "needs": needs, "priority": priority, "model": None, "window": None, "once": when.once,
            "action": action, "when": when.say}
    nxt = next_run(when.cron, now)
    conn.execute("INSERT INTO schedules (id, plugin, workflow, cron, spec, enabled, next_run_at, created_at,"
                 " updated_at, owner, label) VALUES (?,?,?,?,?,1,?,?,?,'you',?)",
                 (sid, plugin, workflow, when.cron, json.dumps(spec, sort_keys=True), nxt, now, now, label[:200]))
    return {"id": sid, "cron": when.cron, "next_run_at": nxt, "label": label}


# "good morning" (and "brief me", "what's my day look like?"): the morning brief, spoken
BRIEF = re.compile(r"^\s*(?:(?:good\s+)?morning(?:[,\s]+ari)?|brief\s+me"
                   r"|what'?s\s+(?:on\s+)?(?:my|the)\s+day(?:\s+look\s+like)?"
                   r"|how'?s\s+my\s+day(?:\s+looking)?)\s*[.!?]*\s*$", re.I)

# ------------------------------------------------------------------ what you asked Ari to remember

WEATHER = re.compile(r"\b(?:weather|forecast|temperature|umbrella|(?:will|is) it (?:rain|be (?:hot|cold|sunny))|"
                     r"raining|going to rain)\b", re.I)
_PLACE = re.compile(r"\b(?:in|at|for)\s+(?P<p>[A-Za-z][\w .,'-]{1,40}?)\s*(?:today|tomorrow|now|right now|"
                    r"this (?:morning|afternoon|evening)|tonight)?\s*[?.!]*\s*$", re.I)


def weather_ask(text: str) -> tuple[str, int] | None:
    """("Kandy" or "" for the usual place, 0 today / 1 tomorrow) when this is a plain weather question."""
    t = text.strip()
    if not WEATHER.search(t) or len(t) > 70 or re.match(r"\W*(?:why|explain|what (?:is|are) (?:a |the )?"
                                                       r"(?:weather|forecast)s?\b|how (?:do|does|are|is) "
                                                       r"(?:the )?(?:weather|forecast))", t, re.I):
        return None
    m = _PLACE.search(t)
    place = m.group("p").strip(" ,.") if m else ""
    if place.lower() in {"the morning", "the evening", "the afternoon", "the weekend", "my area", "here", "home"}:
        place = ""
    return place, 1 if re.search(r"\btomorrow\b", t, re.I) else 0


REMEMBER = re.compile(r"^\s*(?:please\s+)?(?:remember|note|keep in mind|don'?t forget)\s+(?:that\s+)?(?P<fact>.{3,})$",
                      re.I)


def remember(conn: sqlite3.Connection, fact: str) -> int:
    fact = re.sub(r"\s+", " ", fact).strip().rstrip(".")[:500]
    now = time.time()
    row = conn.execute("SELECT id FROM ari_memory WHERE lower(fact) = lower(?)", (fact,)).fetchone()
    if row:
        conn.execute("UPDATE ari_memory SET updated_at = ? WHERE id = ?", (now, row[0]))
        return int(row[0])
    cur = conn.execute("INSERT INTO ari_memory (fact, created_at, updated_at) VALUES (?,?,?)", (fact, now, now))
    return int(cur.lastrowid or 0)


def recall(conn: sqlite3.Connection, query: str, k: int = 6) -> list[dict[str, Any]]:
    """The facts that share words with `query`, best first."""
    words = [w for w in re.findall(r"\w+", query.lower()) if len(w) > 2 and w not in _STOP][:12]
    if not words:
        return []
    q = " OR ".join(f'"{w}"' for w in words)
    rows = conn.execute("SELECT m.id, m.fact, m.updated_at FROM ari_memory_fts f JOIN ari_memory m ON m.id = f.rowid"
                        " WHERE ari_memory_fts MATCH ? ORDER BY bm25(ari_memory_fts) LIMIT ?", (q, k)).fetchall()
    return [{"id": r["id"], "fact": r["fact"],
             "noted": time.strftime("%Y-%m-%d", time.localtime(r["updated_at"]))} for r in rows]


def memories(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [{"id": r["id"], "fact": r["fact"], "noted": time.strftime("%Y-%m-%d", time.localtime(r["updated_at"]))}
            for r in conn.execute("SELECT * FROM ari_memory ORDER BY updated_at DESC")]


def forget(conn: sqlite3.Connection, memory_id: int) -> bool:
    return conn.execute("DELETE FROM ari_memory WHERE id = ?", (int(memory_id),)).rowcount > 0


_STOP = {"the", "and", "for", "you", "your", "what", "when", "where", "who", "how", "is", "are", "was", "my", "me",
         "did", "does", "can", "about", "that", "this", "with", "have", "has", "tell", "please", "ari", "do"}


def chats(conn: sqlite3.Connection, limit: int = 100) -> list[dict]:
    """Every conversation, newest first: its first question (the title), the last thing said, how many turns."""
    rows = conn.execute(
        "SELECT conv, COUNT(*) AS turns, MIN(created_at) AS started, MAX(created_at) AS updated,"
        " (SELECT text FROM ari_turns f WHERE f.conv = t.conv AND f.role = 'you' ORDER BY f.id LIMIT 1) AS title,"
        " (SELECT text FROM ari_turns l WHERE l.conv = t.conv AND l.text IS NOT NULL"
        "  ORDER BY l.id DESC LIMIT 1) AS last"
        " FROM ari_turns t GROUP BY conv ORDER BY updated DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def delete_chat(conn: sqlite3.Connection, conv: str) -> int:
    return conn.execute("DELETE FROM ari_turns WHERE conv = ?", (conv,)).rowcount


def open_questions(conn: sqlite3.Connection, since: float) -> list[dict]:
    """Every chat whose newest Ari turn still waits for your yes / no (for Helios's inbox)."""
    rows = conn.execute(
        "SELECT t.conv, t.id, t.text, t.pending, t.created_at,"
        " (SELECT text FROM ari_turns f WHERE f.conv = t.conv AND f.role = 'you' ORDER BY f.id LIMIT 1) AS title"
        " FROM ari_turns t WHERE t.role = 'ari' AND t.pending IS NOT NULL AND t.created_at >= ?"
        " AND t.id = (SELECT MAX(id) FROM ari_turns x WHERE x.conv = t.conv)"
        " ORDER BY t.created_at DESC", (since,)).fetchall()
    return [{"conv": r["conv"], "turn": r["id"], "text": r["text"], "title": r["title"], "at": r["created_at"]}
            for r in rows]
