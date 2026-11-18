"""The Send agent restarts itself on new code under systemd (Calypso's updater can't restart it), only when idle."""

from __future__ import annotations

import asyncio

from _send_kit import make_agent
from argus.send import reload
from argus.send.progress import Progress


def git(tmp, head: str, refs: dict[str, str] | None = None, packed: str | None = None):
    g = tmp / ".git"
    g.mkdir()
    (g / "HEAD").write_text(head + "\n", encoding="ascii")
    for ref, sha in (refs or {}).items():
        (g / ref).parent.mkdir(parents=True, exist_ok=True)
        (g / ref).write_text(sha + "\n", encoding="ascii")
    if packed:
        (g / "packed-refs").write_text(packed, encoding="ascii")
    return tmp


def test_code_stamp_reads_branch_packed_and_detached(tmp_path):
    (tmp_path / "a").mkdir()
    assert reload.code_stamp(git(tmp_path / "a", "ref: refs/heads/main", {"refs/heads/main": "abc123"})) == "abc123"
    (tmp_path / "b").mkdir()
    b = git(tmp_path / "b", "ref: refs/heads/main", packed="# pack-refs\ndef456 refs/heads/main\n")
    assert reload.code_stamp(b) == "def456"
    (tmp_path / "c").mkdir()
    assert reload.code_stamp(git(tmp_path / "c", "0123abcd")) == "0123abcd"
    assert reload.code_stamp(tmp_path / "missing") is None


def test_should_restart():
    assert not reload.should_restart("a", "a", True, 0)
    assert not reload.should_restart(None, "b", True, 0)
    assert not reload.should_restart("a", None, True, 0)
    assert reload.should_restart("a", "b", True, 0)
    assert not reload.should_restart("a", "b", False, 60)
    assert reload.should_restart("a", "b", False, reload.MAX_WAIT_SECONDS)


def test_idle_tracks_live_transfers_queue_and_pending_clips(tmp_path):
    ag = make_agent(tmp_path, "calypso", "Calypso")
    assert ag.idle()
    ag.live[("o1", "send")] = Progress("o1", "send", "pc", "calypso", "x", 1, 10, now=0.0)
    assert not ag.idle()
    ag.live[("o1", "send")].state = "done"
    assert ag.idle()
    oid = ag.store.add("phone", [])
    ag.store._x("UPDATE queue SET state='sending' WHERE id=?", (oid,))
    assert not ag.idle()
    ag.store._x("UPDATE queue SET state='done' WHERE id=?", (oid,))
    assert ag.idle()
    ag.clip.pending["phone"] = object()
    assert not ag.idle()
    ag.store.close()


def test_run_forever_ends_when_new_code_lands_and_idle(tmp_path, monkeypatch):
    stamps = iter(["old", "old", "new"])
    monkeypatch.setattr(reload, "under_systemd", lambda: True)
    monkeypatch.setattr(reload, "CHECK_SECONDS", 0.01)
    monkeypatch.setattr(reload, "code_stamp", lambda repo=None: next(stamps, "new"))
    ag = make_agent(tmp_path, "calypso", "Calypso")
    ag.reload_on_new_code = True
    asyncio.run(asyncio.wait_for(ag.run_forever(), 20))  # returns: systemd (or respawn on the PC) starts the new code
    assert ag.restart_requested


def test_without_reload_on_new_code_it_keeps_running(tmp_path, monkeypatch):
    monkeypatch.setattr(reload, "CHECK_SECONDS", 0.01)
    n = iter(range(10**6))
    monkeypatch.setattr(reload, "code_stamp", lambda repo=None: f"commit {next(n)}")
    ag = make_agent(tmp_path, "pc", "PC")

    async def go():
        t = asyncio.create_task(ag.run_forever())
        await asyncio.sleep(0.3)
        assert not t.done()
        ag._server.should_exit = True
        for x in ag._tasks[1:]:
            x.cancel()
        await asyncio.gather(t, return_exceptions=True)
    asyncio.run(go())
