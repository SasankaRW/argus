"""Ask Argus: plain words in, an answer and at most one suggested action out.

    "sort downloads"            -> action run:downloads-organizer:sort   (rules, instant)
    "what's running?"           -> the queue, from data                  (rules, instant)
    "did the bill job fail?"    -> a model reads a snapshot of Argus and answers (a job on a worker: T1, then T2)

Nothing is done without a tap: an action comes back as a suggestion and runs only through `POST /ask/do`.
The snapshot the model sees is Argus's own state (queue, recent runs, approvals, power); it can't change anything.
"""

from __future__ import annotations

import re
import time
from typing import Any

STOP = {"the", "a", "an", "my", "me", "please", "now", "can", "you", "could", "would", "to", "and", "of", "in", "on",
        "for", "it", "this", "that", "just", "hey", "argus", "pls", "do", "run", "start"}
QUESTIONS = {
    "queue": re.compile(r"\b(queue|queued|running|in progress|what'?s (running|next|going on)|busy|pending jobs?)\b"),
    "approvals": re.compile(r"\b(approv\w*|waiting for me|needs me|need me|decide)\b"),
    "failures": re.compile(r"\b(fail\w*|dead|errors?|broken|went wrong)\b"),
    "power": re.compile(r"\b(pc|computer) (on|off|awake|asleep|status)\b|\bis the pc\b"),
}
VIEWS = {"queue": "Queue", "runs": "Runs", "logs": "Logs", "power": "Power", "share": "Share", "map": "Live map"}
POWER = {"sleep": "Put the PC to sleep", "shutdown": "Shut the PC down", "restart": "Restart the PC",
         "wake": "Wake the PC", "cancel": "Cancel a pending shutdown"}
POWER_WORDS = {"sleep": r"\b(sleep|suspend)\b", "shutdown": r"\b(shut ?down|turn off|power off|switch off)\b",
               "restart": r"\b(restart|reboot)\b", "wake": r"\b(wake|turn on|power on|switch on)\b",
               "cancel": r"\bcancel (the )?(shut ?down|restart|sleep)\b"}


def words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOP]


def catalog(plugin_host) -> list[dict[str, Any]]:
    """Every action Ask may suggest: plugin buttons, power, and pages to open."""
    out = []
    for pid, p in sorted(plugin_host.plugins.items()):
        for t in p.manifest.triggers:
            if t.manual:
                out.append({"id": f"run:{pid}:{t.manual.workflow}", "label": t.manual.label,
                            "about": f"{p.manifest.name}: {p.manifest.description}"[:200]})
    for a, label in POWER.items():
        out.append({"id": f"power:{a}", "label": label, "about": "PC power"})
    for v, label in VIEWS.items():
        out.append({"id": f"show:{v}", "label": f"Open {label}", "about": "a Helios page"})
    return out


def rules(text: str, actions: list[dict[str, Any]], snap: dict[str, Any]) -> dict[str, Any] | None:
    """T0: the common asks, answered without a model. None when unsure: the model gets it."""
    t = text.lower().strip()
    if not t:
        return None
    for a, rx in POWER_WORDS.items():
        if re.search(rx, t) and re.search(r"\b(pc|computer|machine|desktop)\b", t):
            return {"reply": f"{POWER[a]}?", "action": f"power:{a}"}
    if QUESTIONS["queue"].search(t):
        return {"reply": describe_queue(snap), "action": "show:queue"}
    if QUESTIONS["approvals"].search(t):
        n = snap["approvals"]
        return {"reply": ("Nothing waits for you." if not n else
                          f"{len(n)} waiting for you: " + "; ".join(n[:3])), "action": "show:queue" if n else None}
    if QUESTIONS["failures"].search(t) and len(words(t)) <= 4:
        dead = [r for r in snap["recent"] if r["state"] == "dead"]
        return {"reply": ("No failed jobs lately." if not dead else
                          "Failed lately: " + "; ".join(f"{r['job']} ({r['when']}): {r['error']}" for r in dead[:3])),
                "action": "show:runs" if dead else None}
    # a plugin button, named closely enough: every meaningful word of the ask is in the button or its plugin
    asked = set(words(t))
    full = []
    for a in actions:
        if asked and a["id"].startswith("run:"):
            have = set(words(a["label"] + " " + a["id"].replace(":", " ").replace("-", " ")))
            if asked <= have:
                full.append(a)
    if len(full) == 1:  # exactly one button fits: suggest it (two fit: let the model ask which)
        return {"reply": f"{full[0]['label']}?", "action": full[0]["id"]}
    for v, label in VIEWS.items():
        if re.fullmatch(rf"(open|show|go to)?\s*(the )?{v}( page)?", t):
            return {"reply": f"Opening {label}.", "action": f"show:{v}"}
    return None


def describe_queue(snap: dict[str, Any]) -> str:
    q = snap["queue"]
    if not q["running"] and not q["queued"]:
        return "Nothing running and the queue is empty."
    parts = []
    if q["running"]:
        parts.append("Running: " + ", ".join(q["running"][:3]))
    if q["queued"]:
        parts.append(f"{len(q['queued'])} queued, next: " + ", ".join(q["queued"][:3]))
    return ". ".join(parts) + "."


def ago(t: float, now: float) -> str:
    s = now - t
    return f"{int(s)} s ago" if s < 60 else f"{int(s / 60)} min ago" if s < 3600 else f"{s / 3600:.1f} h ago"


async def snapshot(argus) -> dict[str, Any]:
    """A short, plain picture of Argus for the model: what runs, what waits, what happened lately."""
    now = time.time()
    q = await argus.jobs.queue(await argus.registry.workers())
    recent = await argus.jobs.list_jobs(None, 15)

    def name(j) -> str:
        return f"{j['plugin']}.{j['workflow']}" if isinstance(j, dict) else f"{j.plugin}.{j.workflow}"

    def pending(conn):
        return [r[0] for r in conn.execute("SELECT title FROM approvals WHERE state = 'pending' ORDER BY created_at")]

    power = argus.power.status()
    return {
        "now": time.strftime("%Y-%m-%d %H:%M", time.localtime(now)),
        "queue": {"running": [f"{name(j)} ({j.get('step') or 'starting'})" for j in q["running"]],
                  "queued": [name(j) for j in q["queued"]], "waiting_for_you": [name(j) for j in q["waiting"]]},
        "approvals": await argus.store.read(pending),
        "recent": [{"job": name(j), "state": j.state.value, "when": ago(j.created_at, now),
                    "error": (j.error or "").split("\n")[0][:120]} for j in recent
                   if j.state.value in ("succeeded", "dead", "cancelled")][:10],
        "workers_online": q["workers_online"],
        "power": {"auto": power["state"], "mode": power["mode"]},
    }
