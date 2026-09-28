"""SQLite store: the single source of truth for Argus.

- WAL mode, so reads never block the writer.
- Exactly one writer thread. Every write is a function run inside a transaction on that thread,
  so writes never collide and "database is locked" cannot happen between Argus parts.
- Writes that arrive together are committed together (group commit); each one runs inside its own
  SAVEPOINT, so one failing write never undoes the others.
- Numbered migrations in db/migrations/ are applied at open. Argus refuses to open a database
  that is newer than the code.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import re
import sqlite3
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from pathlib import Path
from typing import Any, TypeVar

log = logging.getLogger("argus.db")

T = TypeVar("T")

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
_MAX_BATCH = 64


class StoreError(Exception):
    """A problem with the database that a human needs to act on."""


def _connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    uri = f"file:{path.as_posix()}{'?mode=ro' if readonly else ''}"
    conn = sqlite3.connect(uri, uri=True, isolation_level=None, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA foreign_keys = ON")
    if not readonly:
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def available_migrations() -> list[tuple[int, str, Path]]:
    found = []
    for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
        m = _MIGRATION_RE.match(f.name)
        if not m:
            raise StoreError(f"Badly named migration file: {f.name} (expected NNNN_name.sql)")
        found.append((int(m.group(1)), m.group(2), f))
    numbers = [n for n, _, _ in found]
    if numbers != list(range(1, len(numbers) + 1)):
        raise StoreError(f"Migrations must be numbered 0001, 0002, ... without gaps; found {numbers}")
    return found


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._write_q: queue.Queue = queue.Queue()
        self._writer: threading.Thread | None = None
        self._writer_conn: sqlite3.Connection | None = None
        self._local = threading.local()
        self._read_conns: list[sqlite3.Connection] = []
        self._read_lock = threading.Lock()
        self._closed = threading.Event()
        self.schema_version = 0
        self.writes_done = 0

    # ---------------------------------------------------------------- lifecycle

    def open(self) -> Store:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = _connect(self.path)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        if mode.lower() != "wal":
            conn.execute("PRAGMA journal_mode = WAL")
        self._migrate(conn)
        self._writer_conn = conn
        self._closed.clear()
        self._writer = threading.Thread(target=self._writer_loop, name="argus-db-writer", daemon=True)
        self._writer.start()
        log.info("store opened", extra={"path": str(self.path), "schema_version": self.schema_version})
        return self

    def close(self) -> None:
        if self._writer is None:
            return
        self._write_q.put(None)
        self._writer.join(timeout=10)
        self._writer = None
        if self._writer_conn is not None:
            self._writer_conn.close()
            self._writer_conn = None
        with self._read_lock:
            for c in self._read_conns:
                try:
                    c.close()
                except sqlite3.Error:
                    pass
            self._read_conns.clear()
        self._local = threading.local()
        self._closed.set()
        log.info("store closed", extra={"path": str(self.path)})

    # ---------------------------------------------------------------- migrations

    def _migrate(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at REAL NOT NULL)"
        )
        applied = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
        migrations = available_migrations()
        code_max = migrations[-1][0] if migrations else 0
        db_max = max(applied) if applied else 0
        if db_max > code_max:
            raise StoreError(
                f"The database at {self.path} is schema version {db_max}, but this Argus only knows "
                f"up to version {code_max}. Upgrade Argus, or restore a backup made by this version."
            )
        for number, name, file in migrations:
            if number in applied:
                continue
            sql = file.read_text(encoding="utf-8")
            try:
                conn.executescript(
                    "BEGIN IMMEDIATE;\n" + sql + "\n"
                    f"INSERT INTO schema_migrations(version, name, applied_at) "
                    f"VALUES ({number}, '{name}', {time.time()});\nCOMMIT;"
                )
            except sqlite3.Error as e:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise StoreError(f"Migration {number:04d}_{name} failed: {e}") from e
            log.info("migration applied", extra={"version": number, "migration": name})
        self.schema_version = code_max

    # ---------------------------------------------------------------- writes

    def _writer_loop(self) -> None:
        conn = self._writer_conn
        assert conn is not None
        while True:
            item = self._write_q.get()
            if item is None:
                return
            batch = [item]
            while len(batch) < _MAX_BATCH:
                try:
                    nxt = self._write_q.get_nowait()
                except queue.Empty:
                    break
                if nxt is None:
                    self._write_q.put(None)  # finish this batch, then stop
                    break
                batch.append(nxt)
            self._run_batch(conn, batch)

    def _run_batch(self, conn: sqlite3.Connection, batch: list[tuple[Callable, Future]]) -> None:
        results: list[tuple[Future, bool, Any]] = []
        try:
            conn.execute("BEGIN IMMEDIATE")
            for i, (fn, fut) in enumerate(batch):
                sp = f"w{i}"
                conn.execute(f"SAVEPOINT {sp}")
                try:
                    value = fn(conn)
                except BaseException as e:  # noqa: BLE001 - the error goes back to the caller
                    conn.execute(f"ROLLBACK TO {sp}")
                    conn.execute(f"RELEASE {sp}")
                    results.append((fut, False, e))
                else:
                    conn.execute(f"RELEASE {sp}")
                    results.append((fut, True, value))
            conn.execute("COMMIT")
        except sqlite3.Error as e:
            if conn.in_transaction:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            log.error("write batch failed", extra={"error": str(e), "size": len(batch)})
            for _, fut in batch:
                if not fut.done():
                    fut.set_exception(StoreError(f"write failed: {e}"))
            return
        self.writes_done += len(batch)
        for fut, ok, value in results:
            if ok:
                fut.set_result(value)
            else:
                fut.set_exception(value)

    def submit(self, fn: Callable[[sqlite3.Connection], T]) -> Future:
        if self._writer is None or not self._writer.is_alive():
            raise StoreError("store is not open")
        fut: Future = Future()
        self._write_q.put((fn, fut))
        return fut

    def write_sync(self, fn: Callable[[sqlite3.Connection], T], timeout: float = 30) -> T:
        """Run fn(conn) in a write transaction and wait for the result (for non-async callers)."""
        return self.submit(fn).result(timeout=timeout)

    async def write(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Run fn(conn) in a write transaction on the writer thread."""
        return await asyncio.wrap_future(self.submit(fn))

    # ---------------------------------------------------------------- reads

    def _read_conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = _connect(self.path)
            conn.execute("PRAGMA query_only = ON")
            self._local.conn = conn
            with self._read_lock:
                self._read_conns.append(conn)
        return conn

    def read_sync(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        return fn(self._read_conn())

    async def read(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        return await asyncio.to_thread(self.read_sync, fn)

    # ---------------------------------------------------------------- health

    def health(self) -> dict[str, Any]:
        writer_ok = self._writer is not None and self._writer.is_alive()
        try:
            self.read_sync(lambda c: c.execute("SELECT 1").fetchone())
            db_ok = True
        except sqlite3.Error:
            db_ok = False
        return {
            "ok": writer_ok and db_ok,
            "writer": "ok" if writer_ok else "stopped",
            "database": "ok" if db_ok else "error",
            "schema_version": self.schema_version,
            "write_queue": self._write_q.qsize(),
        }
