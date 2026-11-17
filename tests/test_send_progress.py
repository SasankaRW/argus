"""A transfer's live progress: smoothed speed, time left once it settles, and reports throttled to ~2 a second."""

from __future__ import annotations

from argus.send.progress import Progress


def test_speed_is_smoothed_and_eta_waits_until_it_settles():
    p = Progress("o1", "send", "pc", "phone", "a.bin", 1, 10_000_000, now=0.0)
    p.update(1_000_000, now=0.5)
    assert p.snapshot()["eta"] is None  # under 2 s: not settled yet
    for i in range(2, 9):
        p.update(1_000_000 * i, now=0.5 * i)
    s = p.snapshot()
    assert 1_800_000 < s["speed"] < 2_200_000  # 1 MB every 0.5 s
    assert 0.5 < s["eta"] < 1.5  # 2 MB left at ~2 MB/s
    assert set(s) == {"offer_id", "leg", "from", "to", "label", "files", "total", "done", "state", "speed", "eta"}


def test_due_throttles_but_state_changes_report_at_once():
    p = Progress("o1", "send", "pc", "phone", "a", 1, 100, now=0.0)
    assert p.due(0.0) and not p.due(0.2) and p.due(0.6)
    p.update(100, now=0.7, state="done")
    assert p.due(0.71) and p.finished
