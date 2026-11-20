"""When Ari says important news out loud (ari_listen.watch feeds it and asks it on every loop).

At the PC (idle under 5 minutes, not paused, inside speak hours, nothing playing): a notice is said straight away, as
before. Otherwise it is kept, newest last, at most five, each for a day, and said as one short line the first time
you're back ("While you were away: Dune book is back in stock."). The 10-minute gap between spoken notices delays
the next one instead of dropping it. Pure: the caller passes the time and whether Ari may speak now."""

from __future__ import annotations

import re

MAX_KEPT = 5
KEEP_S = 86400.0
GAP_S = 600.0


def notice_text(title: str, text: str) -> str:
    """An important phone message, as one or two short spoken sentences."""
    first = re.split(r"(?<=[.!?])\s|\n", " ".join(text.split("\n")[:2]).strip())[0] if text.strip() else ""
    t = title.strip().rstrip(".")
    said = t if not first or first.lower().startswith(t.lower()) else f"{t}. {first}"
    return ("Heads up: " + (said if said else first)).strip()[:220]


def _plain(s: str) -> str:
    return re.sub(r"^(?:Watcher: )?(?:Wishlist: )?", "", s.strip())


def short_of(title: str, text: str) -> str:
    """The news in a few words, for the catch-up line: the message's first sentence, else its title."""
    first = re.split(r"(?<=[.!?])\s|\n", text.strip())[0] if text.strip() else ""
    return _plain(first or title).rstrip(".!")[:120]


def catch_up_line(items: list[str]) -> str:
    """'While you were away: a, b, and c.' (newest first; past three, 'and N more')."""
    shown, more = items[:3], len(items) - 3
    if len(items) == 1:
        body = shown[0]
    elif more > 0:
        body = ", ".join(shown) + f", and {more} more"
    else:
        body = ", ".join(shown[:-1]) + f", and {shown[-1]}"
    return f"While you were away: {body}."


class NoticeKeeper:
    def __init__(self) -> None:
        self.items: list[dict] = []  # {key, spoken, short, at}
        self.was_away = False
        self.last_spoke = -GAP_S

    def add(self, title: str, text: str, now: float) -> None:
        key = (title.strip(), text.strip())
        if any(i["key"] == key and now - i["at"] < KEEP_S for i in self.items):
            return  # the same news again (a retried job): once is enough
        self.items = (self.items + [{"key": key, "spoken": notice_text(title, text), "short": short_of(title, text),
                                     "at": now}])[-MAX_KEPT:]

    def take(self, now: float, here: bool, away: bool | None = None) -> str | None:
        """The sentence to say now, or None. `here`: at the PC and free to speak. `away`: why not, if not: really
        gone (idle, paused, outside speak hours; the default) or only busy for a moment (Ari talking, a call), which
        doesn't make the news "while you were away"."""
        self.items = [i for i in self.items if now - i["at"] < KEEP_S]
        if not here:
            if self.items and (away is None or away):
                self.was_away = True
            return None
        if not self.items or now - self.last_spoke < GAP_S:
            return None
        if self.was_away or len(self.items) > 1:
            line = catch_up_line([i["short"] for i in reversed(self.items)])
        else:
            line = self.items[0]["spoken"]
        self.items, self.was_away, self.last_spoke = [], False, now
        return line
