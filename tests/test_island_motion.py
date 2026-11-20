"""The island's timing: page flips, rows rising in, the page dots, the next job's ring, Ari's orb, ripples."""

from __future__ import annotations

import pytest

from argus.ari import island_motion as im


def test_ease_starts_still_ends_still_and_never_goes_back():
    assert im.ease(0) == 0 and im.ease(1) == 1
    assert im.ease(-1) == 0 and im.ease(2) == 1
    xs = [im.ease(i / 20) for i in range(21)]
    assert all(b >= a for a, b in zip(xs, xs[1:]))
    assert im.ease(0.3) > 0.6  # decelerates: most of the way there early


def test_a_flip_moves_the_old_page_out_and_the_new_one_in():
    old, new = im.flip_frame(0, 1)
    assert old == (0, 1, 1) and new[1] == 0 and new[0] == pytest.approx(36)
    old, new = im.flip_frame(1, 1)
    assert old[1] == 0 and old[0] == pytest.approx(-36) and old[2] == pytest.approx(0.96)
    assert new == (0, 1, 1)
    old, new = im.flip_frame(1, -1)  # backwards: the other way round
    assert old[0] == pytest.approx(36)
    assert im.flip_frame(0, 1, still=True)[1] == (0, 1, 1)  # reduced motion: there at once


def test_rows_rise_in_one_after_another():
    first, third = im.rise(0.05, 0), im.rise(0.05, 2)
    assert first[1] > third[1] and first[0] < third[0]
    assert im.rise(1.0, 5) == (0, 1)
    assert im.rise(0, 3, still=True) == (0, 1)


def test_the_current_dot_stretches():
    assert im.dot_widths(4, 1, 1, 1) == [5, 14, 5, 5]
    mid = im.dot_widths(4, 2, 1, 0.5)
    assert 5 < mid[1] < 14 and 5 < mid[2] < 14 and sum(mid) == pytest.approx(5 * 2 + 14 + 5)
    assert im.dot_widths(4, 2, 1, 0.1, still=True) == [5, 5, 14, 5]


def test_the_next_jobs_ring_and_words():
    assert im.countdown(1000, 1000 + 23 * 60) == (pytest.approx(23 / 60), "23 min")
    assert im.countdown(1000, 1000 + 2 * 3600)[1] == "2 h" and im.countdown(1000, 1000 + 2 * 3600)[0] == 1
    assert im.countdown(1000, 990) == (0, "now")


def test_ari_s_orb_waves():
    hs = im.wave(0.0)
    assert len(hs) == 5 and all(0.25 <= h <= 0.85 for h in hs) and len(set(round(h, 3) for h in hs)) > 1


def test_bars_ease_toward_their_value():
    v = im.approach(0.2, 0.8, 0.15)
    assert 0.2 < v < 0.8 and im.approach(0.2, 0.8, 0.15, half=0.15) == pytest.approx(0.5, abs=0.01)
    assert im.approach(0.5, 0.5, 1) == 0.5


def test_a_ripple_grows_fades_and_ends():
    s0, a0 = im.ripple(0.0)
    s1, a1 = im.ripple(0.3)
    assert s0 < s1 and a0 > a1
    assert im.ripple(0.61) is None
