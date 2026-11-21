"""Clearing the problem and dead-job counts: they count again from the moment you clear them; no job is deleted."""

from __future__ import annotations

from argus import cleared, timeline
from conftest import run

W, CAPS = "worker-a", {"cpu"}


async def _dead(jobs, clock, name):
    job_id, _ = await jobs.enqueue("p", name, needs=["cpu"], max_attempts=1)
    await jobs.claim(W, CAPS)
    await jobs.start(job_id, W)
    await jobs.fail(job_id, W, f"{name} broke")
    return job_id


def test_clearing_resets_problems_and_dead_until_something_new_fails(jobs, store, clock):
    async def go():
        await _dead(jobs, clock, "old")
        assert (await jobs.counts())["dead"] == 1
        n = store.read_sync(lambda c: timeline.problems(c, clock() + 1)["count"])
        assert n == 1
        store.write_sync(lambda c: cleared.clear(c, clock() + 1))
        clock.advance(5)
        assert (await jobs.counts()).get("dead", 0) == 0
        assert store.read_sync(lambda c: timeline.problems(c, clock())["count"]) == 0
        await _dead(jobs, clock, "new")
        assert (await jobs.counts())["dead"] == 1
        assert store.read_sync(lambda c: timeline.problems(c, clock() + 1)["count"]) == 1
        assert store.read_sync(lambda c: c.execute("SELECT COUNT(*) FROM jobs WHERE state = 'dead'").fetchone()[0]) == 2
    run(go())


def test_post_problems_clear_and_the_console_key(tmp_path):
    from argus import console as con
    from test_ari_think import make
    from test_worker import Server, client

    argus = make(tmp_path)
    with Server(argus.open()) as srv:
        assert client(srv.url).post("/problems/clear", {})["cleared_at"] > 0
        assert argus.store.read_sync(cleared.at) > 0

    class Api:
        posted: list = []

        def post(self, path, body=None):
            self.posted.append(path)

    deck = con.Deck.__new__(con.Deck)
    deck.api, deck.data, deck.show_problems = Api(), {"problems": {"count": 3}}, False
    deck.key("c")  # only while the problem details are open
    assert Api.posted == []
    deck.show_problems = True
    deck.key("c")
    assert Api.posted == ["/problems/clear"] and deck.data["problems"] == {} and not deck.show_problems
