"""Ari's mark on the island: a move for each job, flowing from one to the next on springs."""

from __future__ import annotations

from argus.ari.ari_mark import TONES, Mark, mark_state, shape


def test_each_job_has_its_own_move():
    assert mark_state("listening", "") == "listening"
    assert mark_state("thinking", "") == mark_state("starting", "Ari is starting…") == "thinking"
    assert mark_state("working", "searching your files") == "searching"
    assert mark_state("working", "opening it") == "working"
    assert mark_state("speaking", "") == "speaking"
    assert mark_state("done", "Done, it's 21 degrees.") == "done"
    assert mark_state("done", "Sorry, I couldn't reach the lab.") == "failed"
    assert mark_state("idle", "") == mark_state("following", "x") == "ready"
    looks = set()
    for state in TONES:
        pieces, extras = shape(state, 1.0, 12.3)
        assert len(pieces) == 5  # always the same five pieces, so any move can flow into any other
        looks.add((tuple(round(p.y2 - p.y1, 1) for p in pieces if p.o > 0.05), tuple(e[0] for e in extras)))
    assert len(looks) == len(TONES)  # no two moves look alike


def test_moves_flow_on_springs_and_hold_still_under_reduced_motion():
    m = Mark()
    m.step(0.0, 1 / 60)
    m.set("done", 0.0)
    first, _, tone = m.step(1 / 60, 1 / 60)
    assert tone == TONES["done"] and any(p.o > 0.5 for p in first)  # the bars are still fading out: no jump
    for i in range(2, 120):
        settled, extras, _ = m.step(i / 60, 1 / 60)
    assert all(p.o < 0.05 for p in settled) and extras[0][0] == "fill"  # the badge has taken over
    still = Mark("listening")
    a, _, _ = still.step(5.0, 1 / 60, still=True)
    b, _, _ = still.step(9.0, 1 / 60, still=True)
    assert [(p.y1, p.y2) for p in a] == [(p.y1, p.y2) for p in b]  # reduced motion: nothing moves
