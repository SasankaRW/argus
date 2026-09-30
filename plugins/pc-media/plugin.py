"""PC media: volume and play/pause/next for whatever is playing, like the keyboard's media keys.

Windows only (the keys are sent in your session, so Spotify, YouTube in the browser, and the like all follow them).
Setting an exact level presses volume-down until silent, then volume-up (each press is 2 %): no extra software.
"""

from __future__ import annotations

import platform
import time

from argus.worker import Context, PermanentError, workflow

PLUGIN = "pc-media"
WIN = platform.system() == "Windows"
KEYS = {"mute": 0xAD, "down": 0xAE, "up": 0xAF, "next": 0xB0, "previous": 0xB1, "stop": 0xB2, "play_pause": 0xB3}
STEP = 2  # % per volume key press on Windows


def press(key: str, times: int = 1) -> None:
    if not WIN:
        raise PermanentError("media keys work on Windows only")
    import ctypes

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    vk = KEYS[key]
    for _ in range(times):
        user32.keybd_event(vk, 0, 1, 0)  # key down (extended)
        user32.keybd_event(vk, 0, 3, 0)  # key up
        time.sleep(0.01)


def plan_volume(level: int | None, change: str | None) -> list[tuple[str, int]]:
    """The key presses for a request."""
    if level is not None:
        level = max(0, min(100, int(level)))
        return [("down", 50), ("up", round(level / STEP))]
    c = (change or "").strip().lower()
    if c in ("up", "louder", "increase"):
        return [("up", 5)]
    if c in ("down", "quieter", "lower", "decrease"):
        return [("down", 5)]
    if c in ("mute", "unmute", "toggle"):
        return [("mute", 1)]
    raise PermanentError("say a level 0-100, or up, down, mute")


@workflow(PLUGIN, "volume")
def volume(ctx: Context):
    level = ctx.input.get("level")
    presses = plan_volume(int(level) if level not in (None, "") else None, ctx.input.get("change"))

    def go():
        if ctx.dry_run:
            return {"would_press": presses, "dry_run": True}
        for key, n in presses:
            press(key, n)
        return {"volume": level if level not in (None, "") else ctx.input.get("change")}

    return ctx.step("keys", go)


@workflow(PLUGIN, "media")
def media(ctx: Context):
    action = str(ctx.input.get("action") or "").strip().lower().replace(" ", "_").replace("/", "_")
    action = {"play": "play_pause", "pause": "play_pause", "resume": "play_pause", "skip": "next",
              "back": "previous", "prev": "previous"}.get(action, action)
    if action not in ("play_pause", "next", "previous", "stop"):
        raise PermanentError("play_pause, next, previous or stop")

    def go():
        if ctx.dry_run:
            return {"would_press": action, "dry_run": True}
        press(action)
        return {"pressed": action}

    return ctx.step("key", go)
