"""Kill argus mid-write and check nothing is half-written.

A child process enqueues jobs as fast as it can; we kill it hard (no cleanup) and reopen the database.
Every job must have exactly one job.queued event: the job row and its event commit together or not at all.
"""

from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from argus.db import Store

CORE = Path(__file__).resolve().parents[1] / "core"

CHILD = textwrap.dedent(
    """
    import asyncio, sys
    sys.path.insert(0, {core!r})
    from argus.db import Store
    from argus.jobs import JobStore
    from argus.config import JobsConfig

    async def main():
        store = Store({db!r}).open()
        jobs = JobStore(store, JobsConfig(plugin_queue_limit=10**9))
        print("ready", flush=True)
        i = 0
        while True:
            await asyncio.gather(*(jobs.enqueue("crash", "w", {{"i": i + k}}) for k in range(20)))
            i += 20

    asyncio.run(main())
    """
)


@pytest.mark.parametrize("run_for", [0.3, 0.8])
def test_kill_mid_write_leaves_consistent_database(tmp_path, run_for):
    db = tmp_path / "argus.db"
    proc = subprocess.Popen(
        [sys.executable, "-c", CHILD.format(core=str(CORE), db=str(db))],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout.readline().strip() == "ready"
    time.sleep(run_for)
    if os.name == "nt":
        proc.kill()
    else:
        os.kill(proc.pid, signal.SIGKILL)
    proc.wait(timeout=10)

    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    events = conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'job.queued'").fetchone()[0]
    orphans = conn.execute(
        "SELECT COUNT(*) FROM jobs j WHERE NOT EXISTS"
        " (SELECT 1 FROM events e WHERE e.job_id = j.id AND e.kind = 'job.queued')"
    ).fetchone()[0]
    conn.close()
    assert jobs > 0
    assert jobs == events and orphans == 0

    # And Argus opens it normally afterwards.
    s = Store(db).open()
    assert s.health()["ok"]
    s.close()
