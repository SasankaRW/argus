"""What Ari says out loud and when: important news now if you're at the PC, else kept and said in one line when you're
back (20 Nov: the Dune book restock was dropped because nobody was at the PC)."""

from __future__ import annotations

from argus.ari.notices import NoticeKeeper, catch_up_line

DUNE = ("Watcher: Wishlist: Dune book", "Wishlist: Dune book is back in stock at LKR 4,495.5.")


def test_said_at_once_when_here():
    k = NoticeKeeper()
    k.add(*DUNE, now=1000)
    line = k.take(1000, here=True)
    assert line and "Dune book is back in stock" in line and "away" not in line
    assert k.take(1001, here=True) is None  # said once


def test_kept_while_away_and_said_once_back():
    k = NoticeKeeper()
    for i, name in enumerate(["Dune book", "Kindle", "Lamp", "Desk", "Chair"]):
        k.add(f"Watcher: Wishlist: {name}", f"Wishlist: {name} is back in stock.", now=1000 + i)
        assert k.take(1000 + i, here=False) is None
    line = k.take(2000, here=True)
    assert line.startswith("While you were away:") and "and 2 more" in line and line.count("back in stock") == 3
    assert k.take(2700, here=True) is None  # one line, not five


def test_old_news_is_not_said():
    k = NoticeKeeper()
    k.add(*DUNE, now=1000)
    k.take(1000, here=False)
    assert k.take(1000 + 86401, here=True) is None


def test_the_gap_delays_not_drops():
    k = NoticeKeeper()
    k.add("Job failed: backup", "The backup didn't finish.", now=1000)
    assert k.take(1000, here=True)
    k.add(*DUNE, now=1010)
    assert k.take(1010, here=True) is None  # spoke 10 s ago
    assert "Dune book" in k.take(1601, here=True)


def test_the_same_notice_twice_is_said_once():
    k = NoticeKeeper()
    k.add(*DUNE, now=1000)
    k.add(*DUNE, now=1005)  # a retried job
    k.take(1006, here=False)
    line = k.take(2000, here=True)
    assert line.count("Dune book") == 1


def test_catch_up_wording():
    assert catch_up_line(["a"]) == "While you were away: a."
    assert catch_up_line(["a", "b"]) == "While you were away: a, and b."
    assert catch_up_line(["a", "b", "c"]) == "While you were away: a, b, and c."
    assert catch_up_line(["a", "b", "c", "d", "e"]) == "While you were away: a, b, c, and 2 more."
