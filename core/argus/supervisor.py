"""Keeps argusd and the PC worker running, and restarts them on new code (`dev.ps1 up` starts this).

    python -m argus.supervisor [--interval 60] [--no-worker]

- Starts argusd, then the worker (caps desktop, gpu), in the background; their output goes to logs/.
- A process that exits is started again (waiting longer each time it keeps failing).
- Every `--interval` seconds it checks the checked-out commit (`git rev-parse HEAD`). When it changed (a merged pull
  request was pulled), it waits until no job is running (at most 30 minutes), updates the Python packages if
  pyproject.toml changed, and restarts both. Its own log is logs/supervisor.log (Helios > Logs).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from .config import parse_env_file
from .logs import setup_logging

log = logging.getLogger("argus.supervisor")
ROOT = Path.cwd()
WIN = os.name == "nt"


def head() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                              timeout=20).stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        return None


def deps_hash() -> str:
    p = ROOT / "pyproject.toml"
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else ""


def marker_path() -> Path:
    """argusd's "running" marker: removed after a stop we asked for, so it isn't taken for a power cut."""
    try:
        from .config import load_config

        return load_config(ROOT / "argus.yaml").db_path.parent / "argus.running"
    except Exception:
        return ROOT / "data" / "argus.running"


class Child:
    def __init__(self, name: str, args: list[str], marker: Path | None = None):
        self.name, self.args, self.marker = name, args, marker
        self.proc: subprocess.Popen | None = None
        self.fails = 0
        self.next_start = 0.0

    def start(self) -> None:
        logs = ROOT / "logs"
        logs.mkdir(exist_ok=True)
        out = open(logs / f"{self.name}.out", "w")  # noqa: SIM115 - handed to the child
        err = open(logs / f"{self.name}-crash.log", "w")  # noqa: SIM115
        flags = subprocess.CREATE_NO_WINDOW if WIN else 0  # type: ignore[attr-defined]
        try:
            self.proc = subprocess.Popen([sys.executable, *self.args], cwd=ROOT, stdout=out, stderr=err,
                                         creationflags=flags)
        finally:  # the child has its own handles now
            out.close()
            err.close()
        self.started = time.monotonic()
        log.info("started", extra={"proc": self.name, "pid": self.proc.pid})

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self, timeout: float = 15) -> None:
        if not self.alive():
            return
        assert self.proc is not None
        if WIN:  # the whole tree (a claude.cmd -> node child, for example)
            subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"], capture_output=True)
        else:
            self.proc.terminate()
        try:
            self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        if self.marker is not None:  # we stopped it on purpose (Windows can only kill it outright)
            self.marker.unlink(missing_ok=True)
        log.info("stopped", extra={"proc": self.name})

    def check(self) -> None:
        """Start again if it exited: 5 s, then longer while it keeps failing fast (at most 5 minutes)."""
        if self.alive() or time.monotonic() < self.next_start:
            return
        if self.proc is not None:
            code = self.proc.returncode
            quick = time.monotonic() - self.started < 60
            self.fails = self.fails + 1 if quick else 0
            wait = min(300, 5 * 2 ** min(self.fails, 6))
            log.warning("exited; starting again", extra={"proc": self.name, "code": code, "in_seconds": wait})
            self.next_start = time.monotonic() + wait
            self.proc = None
            return
        self.start()


def busy(url: str, token: str | None) -> bool:
    """True while a job is running (so a restart waits). Unknown counts as not busy."""
    req = urllib.request.Request(url + "/queue")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return bool(json.loads(r.read())["running"])
    except Exception:
        return False


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="argus-supervisor")
    p.add_argument("--interval", type=float, default=60, help="seconds between checks for new code")
    p.add_argument("--url", default="http://127.0.0.1:8600")
    p.add_argument("--no-worker", action="store_true", help="argusd only (the laptop, when the PC is the worker)")
    p.add_argument("--no-argusd", action="store_true",
                   help="the worker only (the PC after the move: set ARGUS_URL to the laptop, e.g. http://laptop:8600)")
    args = p.parse_args(argv)
    setup_logging("INFO", ROOT / "logs" / "supervisor.log")
    env = parse_env_file(ROOT / ".env")
    token = os.environ.get("ARGUS_WORKER_TOKEN") or env.get("ARGUS_WORKER_TOKEN")

    children = [] if args.no_argusd else [Child("argusd", ["-m", "argus"], marker_path())]
    if not args.no_worker:
        children.append(Child("worker", ["-m", "argus.worker.cli", "--cap", "desktop", "--cap", "gpu",
                                         "--log-file", "logs/worker.log"]))
    commit, deps = head(), deps_hash()
    log.info("supervising", extra={"commit": (commit or "?")[:10], "processes": [c.name for c in children]})
    for c in children:
        c.check()
        time.sleep(2)  # argusd first
    changed_at: float | None = None
    last_look = time.monotonic()
    try:
        while True:
            for c in children:
                try:
                    c.check()
                except OSError as e:  # could not start it: try again later
                    log.error("could not start", extra={"proc": c.name, "error": str(e)})
                    c.next_start = time.monotonic() + 60
            if time.monotonic() - last_look >= args.interval:
                last_look = time.monotonic()
                now_commit = head()
                if now_commit and commit and now_commit != commit:
                    changed_at = changed_at or time.monotonic()
                    waited = time.monotonic() - changed_at
                    if busy(args.url, token) and waited < 1800:
                        log.info("new code; waiting for running jobs", extra={"commit": now_commit[:10]})
                    else:
                        log.info("new code; restarting", extra={"from": commit[:10], "to": now_commit[:10]})
                        if deps_hash() != deps:
                            log.info("dependencies changed; updating packages")
                            try:
                                r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-e",
                                                    ".[dev,plugins]"], cwd=ROOT, capture_output=True, text=True,
                                                   timeout=900)
                                if r.returncode:
                                    log.error("package update failed", extra={"error": r.stderr[-500:]})
                            except (OSError, subprocess.SubprocessError) as e:
                                log.error("package update failed", extra={"error": str(e)})
                            deps = deps_hash()
                        for c in reversed(children):
                            c.stop()
                        for c in children:
                            c.fails, c.next_start, c.proc = 0, 0.0, None
                            try:
                                c.start()
                            except OSError as e:
                                log.error("could not start", extra={"proc": c.name, "error": str(e)})
                            time.sleep(2)
                        commit, changed_at = now_commit, None
            time.sleep(2)
    except KeyboardInterrupt:
        pass
    finally:
        for c in reversed(children):
            c.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
