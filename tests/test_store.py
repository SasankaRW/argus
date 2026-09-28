from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest

from argus.db import Store, StoreError
from argus.db.store import available_migrations

TABLES = {"jobs", "steps", "events", "approvals", "outbox", "workers", "components", "schedules",
          "plugin_state", "settings"}


def table_names(store: Store) -> set[str]:
    rows = store.read_sync(lambda c: c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall())
    return {r[0] for r in rows}


def test_migrations_create_all_tables(store):
    assert TABLES <= table_names(store)
    assert store.schema_version == available_migrations()[-1][0]


def test_wal_mode(store):
    mode = store.read_sync(lambda c: c.execute("PRAGMA journal_mode").fetchone()[0])
    assert mode.lower() == "wal"


def test_reopen_is_idempotent_and_keeps_data(db_path):
    s = Store(db_path).open()
    s.write_sync(lambda c: c.execute("INSERT INTO settings VALUES ('k', 'v', 0, 0)"))
    s.close()
    s2 = Store(db_path).open()
    value = s2.read_sync(lambda c: c.execute("SELECT value FROM settings WHERE key='k'").fetchone()[0])
    s2.close()
    assert value == "v"


def test_refuses_newer_database(db_path):
    Store(db_path).open().close()
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO schema_migrations VALUES (999, 'future', 0)")
    conn.commit()
    conn.close()
    with pytest.raises(StoreError, match="newer|only knows"):
        Store(db_path).open()


def test_failed_write_does_not_undo_others_in_same_batch(store):
    def good(i):
        return lambda c: c.execute("INSERT INTO settings VALUES (?, 'x', 0, 0)", (f"k{i}",))

    def bad(c):
        c.execute("INSERT INTO settings VALUES ('dup', 'x', 0, 0)")
        c.execute("INSERT INTO settings VALUES ('dup', 'x', 0, 0)")  # primary key clash

    futs = [store.submit(good(1)), store.submit(bad), store.submit(good(2))]
    futs[0].result()
    with pytest.raises(sqlite3.IntegrityError):
        futs[1].result()
    futs[2].result()
    keys = {r[0] for r in store.read_sync(lambda c: c.execute("SELECT key FROM settings").fetchall())}
    assert keys == {"k1", "k2"}  # the bad write left nothing behind, the good ones survived


def test_1000_concurrent_writes_no_lock_errors(store):
    async def main():
        async def one(i):
            await store.write(lambda c: c.execute(
                "INSERT INTO settings VALUES (?, ?, 0, 0)", (f"key{i}", str(i))))
            # Interleave reads with writes from other threads.
            return await store.read(lambda c: c.execute("SELECT COUNT(*) FROM settings").fetchone()[0])

        await asyncio.gather(*(one(i) for i in range(1000)))

    t0 = time.perf_counter()
    asyncio.run(main())
    elapsed = time.perf_counter() - t0
    count = store.read_sync(lambda c: c.execute("SELECT COUNT(*) FROM settings").fetchone()[0])
    assert count == 1000
    assert elapsed < 10, f"1000 writes took {elapsed:.1f}s"


def test_health(store):
    h = store.health()
    assert h["ok"] and h["writer"] == "ok" and h["database"] == "ok"
    store.close()
    assert store.health()["writer"] == "stopped"


def test_write_after_close_is_refused(db_path):
    s = Store(db_path).open()
    s.close()
    with pytest.raises(StoreError):
        s.write_sync(lambda c: None)
