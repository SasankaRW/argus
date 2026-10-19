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
