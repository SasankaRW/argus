"""Ari's conversation thread: where the chat stands right now, so the next message is read in its light.

"open Spotify" -> "play Famous" -> "louder": the second message is about Spotify and the third is its volume, because
the thread says the app in use is Spotify and a song was just asked for. The thread is small and kept per chat:

    topic       what the chat is about now, in a few words (the model's own words, else from the tools)
    app         the app in use (the last one opened or played in)
    task        the job in progress: its goal (the message that started it), the steps done, what's still missing
    mentioned   the last thing of each kind: song, file, person, page, search, mail, ticket, note
    question    Ari's open question (a missing detail, or "shall I ...?")
    promises    what Ari said it would do later ("I'll remind you at 5")

The worker folds each turn into it (`update`) and returns it with the answer; argusd hands the newest one back with
the next message (`ari.thread`). It lapses with the chat's history (half an hour): an old thread doesn't steer a new
question. What private tools found (the screen, the clipboard, mail, money) is never kept, only that they were used.
"""

from __future__ import annotations

import re
import time
from typing import Any

FRESH_S = 30 * 60
STEPS = 4  # steps of a task kept
PROMISES = 3

# tool -> (the app it is about, the kind of thing its argument names, which argument)
_APP_TOOLS = {"open_app": "name", "phone_open_app": "name", "open_on_workstation": "name", "close_app": "name",
              "switch_to_window": "name"}
_MENTION = {
    "play_on_spotify": ("song", "query"), "open_file": ("file", "path"), "search_everything": ("search", "query"),
    "find_file": ("search", "query"), "search_my_files": ("search", "query"), "open_website": ("page", "url"),
    "read_page": ("page", "url"), "phone_open_link": ("page", "url"), "web_search": ("search", "query"),
    "search_in_browser": ("search", "query"), "whatsapp_message": ("person", "to"), "read_email": ("mail", "which"),
    "show_issue": ("ticket", "key"), "move_issue": ("ticket", "key"), "fix_ticket": ("ticket", "key"),
    "fix_status": ("ticket", "key"), "run_fix": ("ticket", "key"), "comment_issue": ("ticket", "key"),
    "edit_issue": ("ticket", "key"), "add_note": ("note", "text"), "find_notes": ("search", "query"),
    "save_for_later": ("page", "url"), "watch_page": ("page", "url"), "weather": ("place", "place"),
    "add_to_list": ("item", "text"), "repo_status": ("repo", "name"),
    "run_routine": ("routine", "name"), "phone_timer": ("timer", "minutes"),
    "save_to_vault": ("note", "title"), "find_in_vault": ("search", "query"),
}
_TOPIC = {
    "play_on_spotify": "music on Spotify", "media_control": "music playing", "set_volume": "", "check_email":
    "your emails", "read_email": "your emails", "whatsapp_message": "a WhatsApp message", "weather": "the weather",
    "web_search": "a web search", "search_in_browser": "a web search", "shopping_list": "the shopping list",
    "add_to_list": "the shopping list", "money_this_month": "money this month", "lab_status": "the home lab",
    "list_issues": "work issues", "work_summary": "work issues", "show_issue": "a work ticket",
    "fix_ticket": "fixing a ticket", "run_fix": "fixing a ticket", "fix_status": "fixing a ticket",
    "find_notes": "your notes", "add_note": "your notes", "recent_notes": "your notes",
    "search_everything": "finding a file", "find_file": "finding a file", "search_my_files": "finding a file",
    "open_file": "a file", "diary_add": "your diary", "diary_read": "your diary", "phone_timer": "a timer",
    "look_at_screen": "what's on the screen", "summarise_clipboard": "what you copied",
}
MUSIC_APPS = ("spotify", "youtube music", "music", "vlc", "apple music", "deezer", "tidal")
_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|~[\\/]|/)[^\s\"'<>|]+\.[A-Za-z0-9]{1,5}")
PROMISE = re.compile(r"\bI(?:'ll| will)\s+((?:remind|tell|let you know|check|ping|ask|wake)[^.!?]{0,80})", re.I)


def empty() -> dict[str, Any]:
    return {"topic": "", "app": "", "task": None, "mentioned": {}, "question": "", "promises": [], "last": None,
            "at": 0.0}


def fresh(th: dict | None, now: float) -> dict[str, Any]:
    """The thread to build on: a copy of `th`, or a new one when there is none or it has lapsed."""
    if not isinstance(th, dict) or now - float(th.get("at") or 0) > FRESH_S:
        return empty()
    out = {**empty(), **th}
    out["mentioned"] = dict(out.get("mentioned") or {})
    out["promises"] = list(out.get("promises") or [])
    out["task"] = dict(out["task"]) if isinstance(out.get("task"), dict) else None
    return out


def _clip(s: Any, n: int = 120) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[:n - 1] + "…"


def _first_path(result: Any) -> str:
    m = _PATH.search(str(result or "")[:3000])
    return m.group(0).rstrip(".,;)]}") if m else ""


def _question(reply: str, pending: Any) -> str:
    """Ari's open question in this answer: a "shall I ...?" waiting for a yes, or a reply that ends asking."""
    reply = _clip(reply, 400)
    if pending or reply.endswith("?"):
        parts = re.split(r"(?<=[.!])\s+", reply)
        return _clip(parts[-1] if parts else reply, 200)
    return ""


def update(prev: dict | None, *, message: str, out: dict | None, now: float | None = None,
           topic: str = "") -> dict[str, Any]:
    """The thread after this turn: `message` was said, `out` is Ari's answer ({reply, used, pending}), `topic` the
    model's own few words for what the chat is about now (if it gave them)."""
    now = time.time() if now is None else now
    th = fresh(prev, now)
    out = out or {}
    used = [u for u in out.get("used") or [] if isinstance(u, dict) and u.get("tool")]
    ok = [u for u in used if "error" not in u]
    reply = str(out.get("reply") or "")
    said_topic = ""
    for u in ok:
        name, args = str(u["tool"]), u.get("args") if isinstance(u.get("args"), dict) else {}
        if name in _APP_TOOLS and args.get(_APP_TOOLS[name]):
            app = _clip(args[_APP_TOOLS[name]], 40)
            if name == "close_app":
                if th["app"].lower() == app.lower():
                    th["app"] = ""
            else:
                th["app"] = app
                th["mentioned"]["app"] = app
                said_topic = f"{app} open"
        if name == "play_on_spotify":
            th["app"] = "Spotify"
        kind_arg = _MENTION.get(name)
        if u.get("private") and not (name == "read_email" and str(args.get("which") or "").isdigit()):
            kind_arg = None  # a private tool's arguments stay out (a sender's name); a mail's number is fine
        if kind_arg and args.get(kind_arg[1]) not in (None, ""):
            th["mentioned"][kind_arg[0]] = _clip(args[kind_arg[1]])
        if name in ("search_everything", "find_file") and not u.get("private"):
            path = _first_path(u.get("result"))
            if path:
                th["mentioned"]["file"] = _clip(path, 200)
        if name == "check_email":
            th["mentioned"]["mail"] = "the list just read out (by number: 1 is the newest)"
        if _TOPIC.get(name):
            said_topic = _TOPIC[name]
    if ok:  # the last tool that worked, with its arguments (a private tool's stay out): "and tomorrow?" repeats it
        u = ok[-1]
        args = u.get("args") if isinstance(u.get("args"), dict) and not u.get("private") else {}
        th["last"] = {"tool": str(u["tool"]), "args": {k: v if isinstance(v, (bool, int, float)) else _clip(v)
                                                        for k, v in args.items() if v not in (None, "")}}
    if topic.strip():
        said_topic = _clip(topic, 60)
    if said_topic:
        th["topic"] = said_topic

    question = _question(reply, out.get("pending"))
    task = th["task"]
    if task and task.get("missing"):  # Ari had asked for something: this message is the answer, the task goes on
        task["steps"] = (task.get("steps") or []) + [u["tool"] for u in used]
        task["steps"] = task["steps"][-STEPS:]
        task["missing"] = question
    elif used or (question and (out.get("pending") or {}).get("kind") != "remember"):  # something new starts
        task = {"goal": _clip(message, 160), "steps": [u["tool"] for u in used][-STEPS:], "missing": question}
        if th["app"]:
            task["app"] = th["app"]
    if task and used and all("error" in u for u in used):
        task["failed"] = used[-1]["tool"]
    elif task:
        task.pop("failed", None)
    th["task"] = task
    th["question"] = question

    for m in PROMISE.finditer(reply):
        p = _clip(m.group(1), 100)
        if p not in th["promises"]:
            th["promises"].append(p)
    th["promises"] = th["promises"][-PROMISES:]
    th["at"] = now
    return th


def brief(th: dict | None, now: float | None = None) -> dict[str, Any]:
    """The thread as the model sees it: only what is set; {} when there is nothing (or it lapsed)."""
    now = time.time() if now is None else now
    t = fresh(th, now)
    out: dict[str, Any] = {}
    for k in ("topic", "app"):
        if t[k]:
            out[k] = t[k]
    if t["app"] and t["app"].lower() in MUSIC_APPS:
        out["music"] = f"{t['app']} is the music app in use: play, pause, louder and song questions are about it"
    if t["task"]:
        task = {k: v for k, v in t["task"].items() if v}
        if task:
            out["task"] = task
    if t["mentioned"]:
        out["mentioned"] = t["mentioned"]
    if t.get("last"):
        out["last_tool"] = t["last"]
    if t["question"]:
        out["your_open_question"] = t["question"]
    if t["promises"]:
        out["you_promised"] = t["promises"]
    if out:
        out["minutes_ago"] = max(0, round((now - float(t["at"] or now)) / 60))
    return out


def words(th: dict | None) -> str:
    """The thread's words, for picking which tools to offer ("louder" after Spotify keeps the media tools)."""
    if not isinstance(th, dict):
        return ""
    bits = [str(th.get("topic") or ""), str(th.get("app") or "")]
    task = th.get("task") or {}
    bits += [str(task.get("goal") or ""), " ".join(task.get("steps") or [])]
    if (th.get("app") or "").lower() in MUSIC_APPS:
        bits.append("music media song volume")
    return " ".join(b for b in bits if b)
