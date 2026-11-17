"""Live Argus Send transfers, kept in memory only (never the events table): what Helios and the island show while
files move. Agents report every ~500 ms; both ends may report the same transfer, so it is merged by offer id and leg
("send", or "hold" for Calypso passing on a held share): the higher byte count wins and a finish from either end
ends that leg. Finished legs stay ~10 s so screens can show the result, then go. A cancel asked for here reaches the
sending device in the reply to its next report."""

from __future__ import annotations

import threading

KEEP_DONE = 10.0
FINISHED = ("done", "failed", "cancelled")


class SendLive:
    def __init__(self) -> None:
        self._items: dict[tuple[str, str], dict] = {}
        self._cancel: set[str] = set()
        self._changes: list[dict] = []  # legs that started or finished since take_changes(): the "send.live" events
        self._lock = threading.Lock()

    def report(self, device: str, items: list[dict], now: float) -> list[str]:
        """Merge a device's snapshots; returns the offer ids this device, as their sender, should cancel."""
        with self._lock:
            for it in items:
                key = (str(it.get("offer_id", "")), str(it.get("leg", "send")))
                old = self._items.get(key)
                new = {**it, "device": device, "updated": now}
                if old:
                    new["done"] = max(int(old.get("done", 0)), int(it.get("done", 0)))
                    if old.get("state") in FINISHED and it.get("state") not in FINISHED:
                        new["state"] = old["state"]  # a late report from the other end doesn't reopen it
                    new.setdefault("started", old.get("started", now))
                else:
                    new["started"] = now
                if old is None:
                    self._changes.append(self._brief(new))
                if new.get("state") in FINISHED and not (old and old.get("state") in FINISHED):
                    new["finished_at"] = now
                    if old is not None:
                        self._changes.append(self._brief(new))
                elif old and "finished_at" in old:
                    new["finished_at"] = old["finished_at"]
                self._items[key] = new
            mine = {k[0] for k, v in self._items.items() if v.get("from") == device and v.get("state") not in FINISHED}
            out = sorted(self._cancel & mine)
            self._cancel -= set(out)
            return out

    def items(self, now: float) -> list[dict]:
        with self._lock:
            for k in [k for k, v in self._items.items() if v.get("state") in FINISHED
                      and now - v.get("finished_at", now) > KEEP_DONE]:
                del self._items[k]
            rows = list(self._items.values())
        return sorted(rows, key=lambda v: (v.get("state") in FINISHED, -v.get("started", 0)))

    @staticmethod
    def _brief(it: dict) -> dict:
        return {k: it.get(k) for k in ("offer_id", "leg", "from", "to", "label", "state")}

    def take_changes(self) -> list[dict]:
        """Legs that started or finished since the last call (rare: Argus writes one small event for each)."""
        with self._lock:
            out, self._changes = self._changes, []
            return out

    def cancel(self, offer_id: str) -> bool:
        with self._lock:
            if not any(k[0] == offer_id and v.get("state") not in FINISHED for k, v in self._items.items()):
                return False
            self._cancel.add(offer_id)
            return True
