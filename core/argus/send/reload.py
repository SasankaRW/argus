"""Restart on new code. On Calypso the Send agent is a systemd service the updater cannot restart (its sudo rule names
only argusd and argus-worker): it exits once idle and Restart=always brings it back. On the PC nothing restarts the
logon task, so it starts a fresh copy of itself (same command line) and exits."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]  # core/argus/send/reload.py -> the checkout (/opt/argus)
CHECK_SECONDS = 60.0
MAX_WAIT_SECONDS = 30 * 60  # a transfer resumes after a restart: never put the new code off longer than this


def code_stamp(repo: Path = REPO) -> str | None:
    """The commit checked out in `repo` (read from .git, no git process), None when it isn't a git checkout."""
    git = repo / ".git"
    try:
        head = (git / "HEAD").read_text(encoding="ascii").strip()
        if not head.startswith("ref: "):
            return head or None
        ref = head[5:]
        f = git / ref
        if f.exists():
            return f.read_text(encoding="ascii").strip() or None
        for line in (git / "packed-refs").read_text(encoding="ascii").splitlines():
            if line.endswith(" " + ref):
                return line.split(" ", 1)[0]
    except (OSError, UnicodeDecodeError):
        return None
    return None


def under_systemd() -> bool:
    return bool(os.environ.get("INVOCATION_ID"))


def should_restart(started: str | None, now: str | None, idle: bool, waited: float) -> bool:
    """New code is checked out and nothing is moving (or it has waited long enough)."""
    if not started or not now or now == started:
        return False
    return idle or waited >= MAX_WAIT_SECONDS


def respawn() -> None:
    """Start this same command again, detached from this process (which is about to exit)."""
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    subprocess.Popen(list(sys.orig_argv), cwd=os.getcwd(), creationflags=flags, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
