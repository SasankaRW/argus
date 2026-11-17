"""A transfer leg's live progress, as the agent reports it to Argus and the notification shows it.

Speed is an exponentially weighted average of the rate between updates; time left appears once the speed has had
about 2 s to settle. `due()` throttles reports to one every half second, but a change of state reports at once.
"""

from __future__ import annotations

FINISHED = ("done", "failed", "cancelled")
SETTLE_S = 2.0
ALPHA = 0.3


class Progress:
    def __init__(self, offer_id: str, leg: str, src: str, dst: str, label: str, files: int, total: int,
                 now: float) -> None:
        self.offer_id, self.leg, self.src, self.dst = offer_id, leg, src, dst
        self.label, self.files, self.total = label, files, max(0, total)
        self.done, self.state, self.speed = 0, "moving", 0.0
        self.started = self._last_t = now
        self._last_done = 0
        self._reported: float | None = None
        self._changed = True

    @property
    def finished(self) -> bool:
        return self.state in FINISHED

    def update(self, done: int, now: float, state: str = "moving") -> None:
        dt = now - self._last_t
        if dt > 0 and done >= self._last_done:
            rate = (done - self._last_done) / dt
            self.speed = rate if self.speed == 0 else ALPHA * rate + (1 - ALPHA) * self.speed
            self._last_t, self._last_done = now, done
        self.done = min(max(self.done, done), self.total) if self.total else done
        if state != self.state:
            self.state, self._changed = state, True

    def due(self, now: float, every: float = 0.5) -> bool:
        if self._changed or self._reported is None or now - self._reported >= every:
            self._reported, self._changed = now, False
            return True
        return False

    def eta(self) -> float | None:
        if self.finished or self.speed <= 0 or self._last_t - self.started < SETTLE_S:
            return None
        return max(0.0, (self.total - self.done) / self.speed)

    def snapshot(self) -> dict:
        eta = self.eta()
        return {"offer_id": self.offer_id, "leg": self.leg, "from": self.src, "to": self.dst, "label": self.label,
                "files": self.files, "total": self.total, "done": self.done, "state": self.state,
                "speed": round(self.speed), "eta": None if eta is None else round(eta, 1)}
