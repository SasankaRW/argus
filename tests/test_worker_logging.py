"""A job's log lines keep what they were given (the error of a failed job)."""

import logging

from argus.worker.runner import JobLog


def test_the_job_log_keeps_the_extras_of_each_line(caplog):
    jlog = JobLog(logging.getLogger("argus.worker"), {"job": "J1", "plugin": "p", "workflow": "w"})
    with caplog.at_level(logging.ERROR, logger="argus.worker"):
        jlog.error("job failed", extra={"error": "ValueError: boom"})
    rec = caplog.records[-1]
    assert rec.error == "ValueError: boom" and rec.job == "J1" and rec.workflow == "w"


def test_a_job_log_line_says_what_happened():
    from argus.worker.runner import outcome

    got = outcome({"plugin": "ari", "input": {"text": "play famous on spotify"}},
                  {"reply": "Couldn't.", "tier": "T1",
                   "used": [{"tool": "play_on_spotify", "result": {"done": False, "problem": "search box not found"}},
                            {"tool": "set_volume", "result": {"ok": True}}]})
    assert got == {"heard": "play famous on spotify", "reply": "Couldn't.", "model": "T1",
                   "tools": "play_on_spotify ✗, set_volume ✓", "problem": "play_on_spotify: search box not found"}
    assert outcome({"plugin": "workstation"}, {"done": False, "problem": "Spotify is open on your desktop"}) == {
        "problem": "Spotify is open on your desktop"}
    assert outcome({"plugin": "x"}, None) == {}
    app = {"done": True, "answer": "Playing.", "trace": [{"kind": "act", "worked": True, "changed": True},
                                                        {"kind": "act", "worked": True, "changed": False},
                                                        {"kind": "done"}]}
    assert outcome({"plugin": "workstation"}, app)["steps"] == "2 steps, 1 did nothing"


def test_the_worker_waits_for_argus_at_boot_instead_of_exiting():
    from argus.worker.cli import register_patiently
    from argus.worker.client import Unreachable

    tries = []

    class W:
        def register(self):
            tries.append(1)
            if len(tries) < 3:
                raise Unreachable("getaddrinfo failed")

    slept = []
    register_patiently(W(), sleep=slept.append)
    assert len(tries) == 3 and slept == [15, 15]
    t = [0.0]

    class Never:
        def register(self):
            raise Unreachable("down")

    import pytest

    with pytest.raises(Unreachable):  # gives up after its wait (the supervisor starts it again)
        register_patiently(Never(), wait_s=30, sleep=lambda s: t.__setitem__(0, t[0] + s), clock=lambda: t[0])
