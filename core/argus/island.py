"""Ari's island on the PC (the click-open part): which widgets it shows, in what order, and your shortcuts.

Kept in the settings table (key "island"); edited in Helios (Ari page > island). A shortcut is an action:
- a Helios page:   show:<view>            (inbox, ari, queue, ...; opens Helios there)
- a plugin button: run:<plugin>:<workflow> (the same as the button on the plugin's page)
- a routine:       routine:<name>          (the routines plugin; its steps were approved when it was saved)
- PC power, the phone: power:<action>, phone:ring (as in Ask)
- a website:       url:https://...         (opened in the default browser)
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from typing import Any

KEY = "island"
WIDGETS: dict[str, str] = {
    "clock": "The time and date",
    "status": "How Argus is doing: running, queued, workers",
    "inbox": "What waits for you",
    "next": "The next thing on the schedule",
    "shortcuts": "Your shortcuts",
    "last": "Ari's last answer",
}
PAGES = {"map": "Map", "inbox": "Inbox", "ari": "Ari", "plugins": "Plugins", "queue": "Queue", "runs": "Runs",
         "models": "Models", "settings": "Settings", "logs": "Logs", "power": "Power", "share": "Share"}
DEFAULT: dict[str, Any] = {
    "widgets": [{"id": "clock", "on": True}, {"id": "status", "on": True}, {"id": "inbox", "on": True},
                {"id": "next", "on": True}, {"id": "shortcuts", "on": True}, {"id": "last", "on": False}],
    "shortcuts": [{"label": "Sort downloads", "action": "run:downloads-organizer:sort"},
                  {"label": "Work mode", "action": "routine:work mode"},
                  {"label": "Inbox", "action": "show:inbox"},
                  {"label": "Helios", "action": "show:map"}],
}
MAX_SHORTCUTS = 8


class IslandError(Exception):
    pass


def load(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (KEY,)).fetchone()
    try:
        saved = json.loads(row[0]) if row and row[0] else None
    except ValueError:
        saved = None
    if not saved:
        return json.loads(json.dumps(DEFAULT))
    known = [w for w in saved.get("widgets", []) if w.get("id") in WIDGETS]
    have = {w["id"] for w in known}
    known += [w for w in DEFAULT["widgets"] if w["id"] not in have]  # widgets added in a later version
    return {"widgets": known, "shortcuts": saved.get("shortcuts", [])}


def check(cfg: dict[str, Any], actions: set[str]) -> dict[str, Any]:
    """Only known widgets and actions; labels short; at most MAX_SHORTCUTS. Raises IslandError with the reason."""
    widgets = []
    for w in cfg.get("widgets") or []:
        if w.get("id") not in WIDGETS:
            raise IslandError(f"no widget {w.get('id')!r}")
        widgets.append({"id": w["id"], "on": bool(w.get("on"))})
    shortcuts = []
    for s in cfg.get("shortcuts") or []:
        label = str(s.get("label") or "").strip()[:24]
        action = str(s.get("action") or "").strip()
        if not label:
            raise IslandError("a shortcut needs a name")
        if action.startswith("url:"):
            if not re.fullmatch(r"url:https?://[^\s]{3,500}", action):
                raise IslandError(f"{label}: a website must start with http:// or https://")
        elif action.startswith("routine:"):
            if not action[8:].strip():
                raise IslandError(f"{label}: which routine?")
        elif action not in actions:
            raise IslandError(f"{label}: unknown action {action!r}")
        shortcuts.append({"label": label, "action": action})
    if len(shortcuts) > MAX_SHORTCUTS:
        raise IslandError(f"at most {MAX_SHORTCUTS} shortcuts")
    return {"widgets": widgets, "shortcuts": shortcuts}


def save(conn: sqlite3.Connection, cfg: dict[str, Any]) -> None:
    now = time.time()
    conn.execute("INSERT INTO settings (key, value, created_at, updated_at) VALUES (?, ?, ?, ?)"
                 " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                 (KEY, json.dumps(cfg), now, now))


def routines(conn: sqlite3.Connection) -> list[str]:
    row = conn.execute("SELECT value FROM plugin_state WHERE plugin = 'routines' AND key = 'routines'").fetchone()
    if row is None:
        return ["work mode"]  # the routines plugin's default until you save your own
    try:
        return sorted(r.get("name", k) for k, r in (json.loads(row[0]) or {}).items())
    except (ValueError, AttributeError):
        return []
