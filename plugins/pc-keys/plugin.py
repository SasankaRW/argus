"""PC keyboard: Ari types text or presses a shortcut in the window in front. Always asks you first (risky).

Uses Windows' SendInput with Unicode characters (any language, emoji too), so no keyboard layout problems.
Never used for passwords: a text that looks like one is refused.
"""

from __future__ import annotations

import platform
import re
import time

from argus.worker import Context, PermanentError, workflow

PLUGIN = "pc-keys"
WIN = platform.system() == "Windows"
MAX_TEXT = 2000
VK = {"ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B, "windows": 0x5B,
      "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "space": 0x20, "backspace": 0x08,
      "delete": 0x2E, "del": 0x2E, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
      "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28, "insert": 0x2D, "printscreen": 0x2C,
      **{f"f{i}": 0x6F + i for i in range(1, 13)}}
MODIFIERS = {0x11, 0x10, 0x12, 0x5B}
SECRET = re.compile(r"(?i)\b(pass(word)?|pwd|pin|otp|secret|token|api[_ -]?key)\b")


def parse_keys(spec: str) -> list[int]:
    """"ctrl+shift+t" -> virtual-key codes, modifiers first. PermanentError for anything unknown."""
    codes = []
    for part in [p.strip().lower() for p in spec.replace(" ", "").split("+") if p.strip()]:
        if part in VK:
            codes.append(VK[part])
        elif len(part) == 1 and part.isalnum():
            codes.append(ord(part.upper()))
        else:
            raise PermanentError(f"unknown key {part!r}")
    if not codes:
        raise PermanentError("which keys?")
    if all(c in MODIFIERS for c in codes):
        raise PermanentError("a shortcut needs a key besides ctrl/shift/alt/win")
    return sorted(codes, key=lambda c: c not in MODIFIERS)


_TYPES: tuple | None = None


def _types():
    """The Windows INPUT structures (made once, on first use)."""
    global _TYPES
    if _TYPES is None:
        import ctypes
        from ctypes import wintypes

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        class _U(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("u", _U)]

        _TYPES = (INPUT, KEYBDINPUT, _U)
    return _TYPES


def _key(vk: int, scan: int, flags: int):
    INPUT, KEYBDINPUT, U = _types()  # noqa: N806
    u = U()
    u.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
    return INPUT(1, u)  # 1: keyboard


def _send(events: list) -> None:
    import ctypes

    INPUT = _types()[0]  # noqa: N806
    arr = (INPUT * len(events))(*events)
    ctypes.windll.user32.SendInput(len(events), arr, ctypes.sizeof(INPUT))  # type: ignore[attr-defined]


KEYUP, UNICODE = 2, 4


def type_unicode(text: str) -> None:
    for ch in text:
        if ch == "\n":
            _send([_key(0x0D, 0, 0), _key(0x0D, 0, KEYUP)])
        else:
            units = ch.encode("utf-16-le")
            for i in range(0, len(units), 2):
                code = int.from_bytes(units[i:i + 2], "little")
                _send([_key(0, code, UNICODE), _key(0, code, UNICODE | KEYUP)])
        time.sleep(0.004)


def press(codes: list[int]) -> None:
    _send([_key(c, 0, 0) for c in codes] + [_key(c, 0, KEYUP) for c in reversed(codes)])


@workflow(PLUGIN, "type")
def type_text(ctx: Context):
    text = str(ctx.input.get("text") or "")
    if not text:
        raise PermanentError("nothing to type")
    if len(text) > MAX_TEXT:
        raise PermanentError(f"too long to type (at most {MAX_TEXT} characters): use the clipboard")
    if SECRET.search(text):
        raise PermanentError("I don't type passwords, PINs or keys")

    def go():
        if ctx.dry_run:
            return {"would_type": text[:200], "dry_run": True}
        if not WIN:
            raise PermanentError("typing works on Windows only")
        time.sleep(0.3)  # let the window in front settle
        type_unicode(text)
        return {"typed": len(text)}

    return ctx.step("type", go)


@workflow(PLUGIN, "keys")
def keys(ctx: Context):
    spec = str(ctx.input.get("keys") or "")
    codes = parse_keys(spec)

    def go():
        if ctx.dry_run:
            return {"would_press": spec, "dry_run": True}
        if not WIN:
            raise PermanentError("shortcuts work on Windows only")
        time.sleep(0.3)
        press(codes)
        return {"pressed": spec}

    return ctx.step("keys", go)
