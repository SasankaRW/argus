"""Nightly database backups, checked by restoring them, plus a copy on another machine.

- At `backup.at` (local time) argusd copies the database with SQLite's online backup (safe while running) to
  `<data>/backups/argus-YYYYMMDD-HHMM.db`, keeps the newest `backup.keep`, and proves each one: it opens the copy,
  runs an integrity check and counts the jobs. A backup that fails the check is reported and not counted.
- The PC's worker then fetches the newest one (`GET /backups/latest`) into `backup.copy_to` on the PC (a job of
  the built-in plugin "backup"), so a dead laptop disk loses nothing. Last `keep` kept there too.
- `argusd --restore FILE|latest` puts a backup back (argusd stopped first): the backup is checked, one made by a
  newer Argus is refused, and the database it replaces is moved to `<data>/replaced/`, never deleted.
  docs/restore.md has the steps.
"""

from __future__ import annotations

import logging
import socket
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("argus.backup")


class Backups:
    def __init__(self, db_path: Path, keep: int = 5, clock=time.time):
        self.db = db_path
        self.dir = db_path.parent / "backups"
        self.keep = keep
        self.clock = clock

    def make(self) -> dict[str, Any]:
        """Back up now, check the copy, prune old ones. Returns what happened."""
        self.dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.fromtimestamp(self.clock()).strftime("%Y%m%d-%H%M%S")
        target = self.dir / f"argus-{stamp}.db"
        part = self.dir / f"argus-{stamp}.part"  # renamed to .db only once it passed its check
        try:
            src = sqlite3.connect(self.db)
            try:
                dst = sqlite3.connect(part)
                try:
                    src.backup(dst)
                finally:
                    dst.close()
            finally:
                src.close()
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        check = self.verify(part)
        if not check["ok"]:
            bad = target.with_suffix(".bad")
            part.replace(bad)
            log.error("backup failed its check", extra={"file": bad.name, "problem": check.get("problem")})
            return {"ok": False, "file": bad.name, **check}
        part.replace(target)
        removed = self.prune()
        log.info("backup made", extra={"file": target.name, "size": target.stat().st_size, "jobs": check["jobs"]})
        return {"ok": True, "file": target.name, "size": target.stat().st_size, "removed": removed, **check}

    @staticmethod
    def verify(path: Path) -> dict[str, Any]:
        """Restore test: the copy opens, passes SQLite's integrity check and has the tables with rows we expect."""
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
                jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
                version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            finally:
                conn.close()
        except sqlite3.Error as e:
            return {"ok": False, "problem": str(e)}
        if ok != "ok":
            return {"ok": False, "problem": f"integrity check: {ok}"}
        return {"ok": True, "jobs": jobs, "schema": version}

    def list(self) -> list[dict[str, Any]]:
        if not self.dir.is_dir():
            return []
        out = []
        for p in sorted(self.dir.glob("argus-*.db"), reverse=True):
            st = p.stat()
            try:  # when it was made: from its name (the file time changes when it is copied)
                made = datetime.strptime(p.stem[6:], "%Y%m%d-%H%M%S").timestamp()
            except ValueError:
                made = st.st_mtime
            out.append({"file": p.name, "size": st.st_size, "made_at": made})
        return out

    def latest(self) -> Path | None:
        files = sorted(self.dir.glob("argus-*.db")) if self.dir.is_dir() else []
        return files[-1] if files else None

    def prune(self) -> list[str]:
        files = sorted(self.dir.glob("argus-*.db"))
        old = files[: max(0, len(files) - self.keep)]
        for p in old:
            p.unlink(missing_ok=True)
        for p in sorted(self.dir.glob("argus-*.bad"))[:-2]:  # keep the last two failed ones to look at
            p.unlink(missing_ok=True)
        return [p.name for p in old]


class RestoreError(Exception):
    pass


def argusd_listening(host: str, port: int, timeout: float = 0.5) -> bool:
    """Is something answering on argusd's port? (0.0.0.0 and :: are asked on this machine.)"""
    ask = {"0.0.0.0": "127.0.0.1", "::": "::1", "": "127.0.0.1"}.get(host, host)
    try:
        with socket.create_connection((ask, port), timeout=timeout):
            return True
    except OSError:
        return False


def restore(backup: Path, db_path: Path, code_schema: int, clock=time.time) -> dict[str, Any]:
    """Put `backup` back as the database at `db_path`. Call it with argusd stopped.

    The backup must pass `Backups.verify` and must not be newer than this code's schema (an older one is fine:
    argusd migrates it forward on its next start). The current database and its -wal/-shm files are moved to
    `<data>/replaced/<name>-<stamp>/`, never deleted. The copy is written beside the database, checked again,
    then renamed into place, so a failure part way leaves the old database where it was."""
    backup = Path(backup)
    if not backup.is_file():
        raise RestoreError(f"no backup at {backup}")
    check = Backups.verify(backup)
    if not check["ok"]:
        raise RestoreError(f"{backup.name} failed its check: {check.get('problem')}")
    if (check.get("schema") or 0) > code_schema:
        raise RestoreError(f"{backup.name} is schema version {check['schema']}, but this Argus only knows up to "
                           f"{code_schema}. Upgrade Argus first, or pick an older backup.")
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    part = db_path.with_name(db_path.name + ".restoring")
    scraps = [part.with_name(part.name + s) for s in ("", "-wal", "-shm")]
    for p in scraps:  # leftovers from a restore that stopped part way: ours, never the database
        p.unlink(missing_ok=True)
    src = sqlite3.connect(f"file:{backup}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(part)
        try:
            src.backup(dst)
            # one self-contained file (no -wal beside it) to rename into place; argusd turns WAL on again
            dst.execute("PRAGMA journal_mode=DELETE")
        finally:
            dst.close()
    finally:
        src.close()
    again = Backups.verify(part)
    for p in scraps[1:]:
        p.unlink(missing_ok=True)
    if not again["ok"]:
        part.unlink(missing_ok=True)
        raise RestoreError(f"the restored copy failed its check: {again.get('problem')}")
    moved: list[str] = []
    stamp = datetime.fromtimestamp(clock()).strftime("%Y%m%d-%H%M%S")
    aside = db_path.parent / "replaced" / f"{db_path.stem}-{stamp}"
    old = [db_path.with_name(db_path.name + suffix) for suffix in ("", "-wal", "-shm")]
    if any(p.exists() for p in old):
        aside.mkdir(parents=True, exist_ok=True)
        for p in old:
            if p.exists():
                p.replace(aside / p.name)
                moved.append(p.name)
    part.replace(db_path)
    log.info("backup restored", extra={"file": backup.name, "jobs": again["jobs"], "schema": again["schema"],
                                       "replaced": str(aside) if moved else None})
    return {"ok": True, "file": backup.name, "jobs": again["jobs"], "schema": again["schema"],
            "replaced": str(aside) if moved else None, "moved": moved}
