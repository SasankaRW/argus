"""constraints.txt: one lock for every install, and the helper that keeps it."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("lock", ROOT / "scripts" / "lock.py")
lock = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lock)

OLD = """fastapi==0.141.1
    # via argus
numpy==2.4.6 ; python_full_version < '3.12'
numpy==2.5.3 ; python_full_version >= '3.12'
pyvda==0.6.0 ; sys_platform == 'win32'
"""
NEW = """fastapi==0.162.6
numpy==2.4.6 ; python_full_version < '3.12'
numpy==2.5.4 ; python_full_version >= '3.12'
rich==15.0.0
"""


def test_pins_keep_markers_apart():
    p = lock.pins(OLD)
    assert p[("fastapi", "")] == "0.141.1"
    assert p[("numpy", "; python_full_version >= '3.12'")] == "2.5.3"
    assert len(p) == 4


def test_changes_say_what_moves():
    assert lock.changes(OLD, NEW) == [
        "fastapi: 0.141.1 -> 0.162.6",
        "numpy (python_full_version >= '3.12'): 2.5.3 -> 2.5.4",
        "pyvda (sys_platform == 'win32'): 0.6.0 -> gone",
        "rich: new, 15.0.0",
    ]
    assert lock.changes(OLD, OLD) == []


def test_the_header_is_written_once():
    once = lock.with_header(NEW)
    assert once.startswith(lock.HEADER) and lock.with_header(once) == once


def test_the_lock_covers_what_pyproject_needs():
    text = (ROOT / "constraints.txt").read_text(encoding="utf-8")
    assert text.startswith(lock.HEADER)
    names = {name for name, _ in lock.pins(text)}
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    deps = re.search(r"^dependencies = \[(.*?)^\]", pyproject, re.S | re.M).group(1)
    for dep in re.findall(r'^\s*"([A-Za-z0-9_.-]+)', deps, re.M):  # one per line; comments may quote words too
        assert dep.lower() in names, f"{dep} is in pyproject.toml but not in constraints.txt: python scripts/lock.py"
