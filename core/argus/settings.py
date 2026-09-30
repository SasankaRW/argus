"""Argus's own settings, changed in Helios instead of argus.yaml.

argus.yaml stays the base. A change made in Helios is kept as an override (the settings table, key
"argus_overrides") and applied over the running config at once and again at every start; "back to argus.yaml"
removes it. Only settings that take effect without a restart are offered here.

Each change is checked by the same rules as argus.yaml (the config models), so a bad value is refused with the
reason in plain words and nothing is changed.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Literal, get_args, get_origin

from pydantic import BaseModel, ValidationError

KEY = "argus_overrides"

# (setting, group, label). Order is the order on the page.
FIELDS: list[tuple[str, str, str]] = [
    ("ntfy.quiet", "Phone", "Quiet phone: plugins' ordinary messages wait for the evening summary"),
    ("brief.enabled", "Phone", "Morning brief on the phone"),
    ("brief.at", "Phone", "Morning brief at"),
    ("brief.weather", "Phone", "Weather in the brief for (a town, e.g. Colombo; empty for none)"),
    ("summary.enabled", "Phone", "Evening summary on the phone"),
    ("summary.at", "Phone", "Evening summary at"),
    ("approvals.remind_hours", "Approvals", "Remind me about a waiting approval after (hours)"),
    ("approvals.expire_hours", "Approvals", "An approval nobody answers counts as \"no\" after (hours)"),
    ("claude.calls_per_day", "Models", "Claude calls per day, all of Argus"),
    ("guidance.enabled", "Models", "Nightly review of the local models' mistakes (uses Claude)"),
    ("guidance.at", "Models", "Nightly review at"),
    ("power.mode", "Power", "Power: \"simulated\" only says what it would do; \"real\" wakes and shuts down the PC"),
    ("power.idle_minutes", "Power", "Shut the PC down after this long with nothing to do (minutes)"),
    ("backup.enabled", "Backups", "Back up Argus's database every night"),
    ("backup.at", "Backups", "Back up at"),
    ("backup.keep", "Backups", "Backups to keep"),
    ("ari.pill", "Ari", "Ari's pill look"),
]
NAMES = {k for k, _, _ in FIELDS}


class SettingError(Exception):
    pass


def _section(cfg: Any, key: str) -> tuple[BaseModel, str]:
    sec, name = key.split(".", 1)
    return getattr(cfg, sec), name


def describe(model: BaseModel, name: str) -> dict[str, Any]:
    """The kind of value a setting takes, for the form: bool, time, choice (with options), int, number."""
    f = type(model).model_fields[name]
    ann = f.annotation
    out: dict[str, Any] = {}
    if ann is bool:
        out["type"] = "bool"
    elif get_origin(ann) is Literal:
        out["type"], out["options"] = "choice", list(get_args(ann))
    elif ann in (int, float):
        out["type"] = "int" if ann is int else "number"
        for m in f.metadata:
            for attr, k in (("ge", "min"), ("gt", "min"), ("le", "max"), ("lt", "max")):
                if getattr(m, attr, None) is not None:
                    out[k] = getattr(m, attr)
    elif any(getattr(m, "pattern", None) for m in f.metadata):
        out["type"] = "time"
    else:
        out["type"] = "text"
    return out


def base_values(cfg: Any) -> dict[str, Any]:
    """The values argus.yaml gave (taken once, before any override is applied)."""
    return {k: getattr(*_section(cfg, k)) for k in NAMES}


def check(cfg: Any, key: str, value: Any) -> Any:
    """The value as argus.yaml would take it, or SettingError with the reason."""
    if key not in NAMES:
        raise SettingError(f"{key} can't be changed here")
    model, name = _section(cfg, key)
    try:
        fixed = type(model).model_validate({**model.model_dump(), name: value})
    except ValidationError as e:
        err = e.errors()[0]
        raise SettingError(f"{key}: {err.get('msg', 'not a valid value')}") from None
    return getattr(fixed, name)


def apply(cfg: Any, key: str, value: Any, on_change: Any = None) -> None:
    """Change the running config in place (every part of Argus holds the same config objects)."""
    model, name = _section(cfg, key)
    setattr(model, name, value)
    if on_change:
        on_change(key, value)


def load(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (KEY,)).fetchone()
    try:
        return json.loads(row[0]) if row and row[0] else {}
    except ValueError:
        return {}


def save(conn: sqlite3.Connection, overrides: dict[str, Any]) -> None:
    now = time.time()
    conn.execute("INSERT INTO settings (key, value, created_at, updated_at) VALUES (?, ?, ?, ?)"
                 " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                 (KEY, json.dumps(overrides), now, now))


def listing(cfg: Any, base: dict[str, Any], overrides: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for key, group, label in FIELDS:
        model, name = _section(cfg, key)
        out.append({"key": key, "group": group, "label": label, **describe(model, name),
                    "value": getattr(model, name), "default": base[key], "changed": key in overrides})
    return out
