"""Messages to one window, and its picture (P7 part 4). Windows only.

- `capture(hwnd)`: the window's picture even when it's covered or on Ari's Workstation (PrintWindow with
  PW_RENDERFULLCONTENT), in memory only.
- `click(hwnd, x, y)`: a click sent to that window as messages (WM_LBUTTONDOWN / UP at a point), not through your
  mouse: the cursor doesn't move and your clicks elsewhere carry on. Older apps (Win32, WinForms, many dialogs)
  take these; Chromium, Electron and UWP apps usually ignore them, so the result is always checked.
- `type_text(hwnd, text)` / `key(hwnd, vk)`: characters and keys sent to the window's focused control, not to your
  keyboard focus.
"""

from __future__ import annotations

import io
import sys

WM_SETTEXT, WM_KEYDOWN, WM_KEYUP, WM_CHAR = 0x000C, 0x0100, 0x0101, 0x0102
WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, MK_LBUTTON = 0x0200, 0x0201, 0x0202, 0x0001
PW_RENDERFULLCONTENT = 0x2
CWP_SKIPINVISIBLE, CWP_SKIPDISABLED, CWP_SKIPTRANSPARENT = 0x1, 0x2, 0x4


def lparam(x: int, y: int) -> int:
    """MAKELPARAM for a client point (each 16 bits, negative allowed)."""
    return ((y & 0xFFFF) << 16) | (x & 0xFFFF)


def _api():  # pragma: no cover - Windows only
    if sys.platform != "win32":
        raise RuntimeError("window messages work on Windows only")
    import ctypes
    from ctypes import wintypes

    u, g = ctypes.windll.user32, ctypes.windll.gdi32
    try:
        u.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # per-monitor v2: real pixels everywhere
    except Exception:  # noqa: BLE001 - set already, or an older Windows
        pass
    return ctypes, wintypes, u, g


def window_rect(hwnd: int) -> tuple[int, int, int, int]:  # pragma: no cover
    ctypes, wintypes, u, _ = _api()
    r = wintypes.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def capture(hwnd: int) -> tuple[bytes, tuple[int, int, int, int]]:  # pragma: no cover
    """(PNG bytes, the window's screen rectangle)."""
    from PIL import Image

    ctypes, wintypes, u, g = _api()
    rect = window_rect(hwnd)
    w, h = rect[2] - rect[0], rect[3] - rect[1]
    if w <= 0 or h <= 0:
        raise RuntimeError("the window has no size (minimised?)")
    hdc = u.GetWindowDC(hwnd)
    mem = g.CreateCompatibleDC(hdc)
    bmp = g.CreateCompatibleBitmap(hdc, w, h)
    g.SelectObject(mem, bmp)
    try:
        if not u.PrintWindow(hwnd, mem, PW_RENDERFULLCONTENT):
            raise RuntimeError("Windows wouldn't draw that window")

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                        ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                        ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                        ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                        ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]

        bi = BITMAPINFOHEADER()
        bi.biSize, bi.biWidth, bi.biHeight, bi.biPlanes, bi.biBitCount = ctypes.sizeof(bi), w, -h, 1, 32
        buf = ctypes.create_string_buffer(w * h * 4)
        g.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), 0)
        img = Image.frombuffer("RGBA", (w, h), buf.raw, "raw", "BGRA", 0, 1).convert("RGB")
    finally:
        g.DeleteObject(bmp)
        g.DeleteDC(mem)
        u.ReleaseDC(hwnd, hdc)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue(), rect


def _target(hwnd: int, sx: int, sy: int) -> tuple[int, int, int]:  # pragma: no cover
    """The deepest child window under a screen point, and the point in its client coordinates."""
    ctypes, wintypes, u, _ = _api()
    cur = hwnd
    while True:
        pt = wintypes.POINT(sx, sy)
        u.ScreenToClient(cur, ctypes.byref(pt))
        child = u.ChildWindowFromPointEx(cur, pt, CWP_SKIPINVISIBLE | CWP_SKIPDISABLED | CWP_SKIPTRANSPARENT)
        if not child or child == cur:
            return cur, pt.x, pt.y
        cur = child


def click(hwnd: int, sx: int, sy: int) -> None:  # pragma: no cover
    _, _, u, _ = _api()
    target, x, y = _target(hwnd, sx, sy)
    lp = lparam(x, y)
    u.PostMessageW(target, WM_MOUSEMOVE, 0, lp)
    u.PostMessageW(target, WM_LBUTTONDOWN, MK_LBUTTON, lp)
    u.PostMessageW(target, WM_LBUTTONUP, 0, lp)


def _focused(hwnd: int) -> int:  # pragma: no cover
    """The control that has the keyboard inside that window's thread (not your focus elsewhere)."""
    ctypes, wintypes, u, _ = _api()

    class GUITHREADINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD), ("hwndActive", wintypes.HWND),
                    ("hwndFocus", wintypes.HWND), ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                    ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND), ("rcCaret", wintypes.RECT)]

    info = GUITHREADINFO()
    info.cbSize = ctypes.sizeof(info)
    tid = u.GetWindowThreadProcessId(hwnd, None)
    if u.GetGUIThreadInfo(tid, ctypes.byref(info)) and info.hwndFocus:
        return int(info.hwndFocus)
    return hwnd


def type_text(hwnd: int, text: str) -> None:  # pragma: no cover
    _, _, u, _ = _api()
    target = _focused(hwnd)
    for ch in text:
        u.PostMessageW(target, WM_CHAR, ord(ch), 1)


def key(hwnd: int, vk: int) -> None:  # pragma: no cover
    _, _, u, _ = _api()
    target = _focused(hwnd)
    u.PostMessageW(target, WM_KEYDOWN, vk, 1)
    u.PostMessageW(target, WM_KEYUP, vk, 0xC0000001)


def idle_seconds() -> float:  # pragma: no cover
    """How long since you last touched the mouse or keyboard."""
    ctypes, wintypes, u, _ = _api()

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    li = LASTINPUTINFO()
    li.cbSize = ctypes.sizeof(li)
    if not u.GetLastInputInfo(ctypes.byref(li)):
        return 0.0
    return max(0.0, (ctypes.windll.kernel32.GetTickCount() - li.dwTime) / 1000.0)


def real_click(sx: int, sy: int) -> None:  # pragma: no cover
    """The last resort: the real mouse, one click at a screen point, then the cursor goes back where it was. Only
    called when you're away or said yes (the task loop checks), with the window shown in front."""
    ctypes, wintypes, u, _ = _api()

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_void_p)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("mi", MOUSEINPUT)]  # the mouse is the union's largest member

    vx, vy = u.GetSystemMetrics(76), u.GetSystemMetrics(77)  # the virtual screen (all monitors)
    vw, vh = u.GetSystemMetrics(78), u.GetSystemMetrics(79)
    ax, ay = round((sx - vx) * 65535 / max(1, vw - 1)), round((sy - vy) * 65535 / max(1, vh - 1))
    was = wintypes.POINT()
    u.GetCursorPos(ctypes.byref(was))
    move, down, up, absolute, virtual = 0x0001, 0x0002, 0x0004, 0x8000, 0x4000
    seq = (INPUT * 3)(INPUT(0, MOUSEINPUT(ax, ay, 0, move | absolute | virtual, 0, None)),
                      INPUT(0, MOUSEINPUT(ax, ay, 0, down | absolute | virtual, 0, None)),
                      INPUT(0, MOUSEINPUT(ax, ay, 0, up | absolute | virtual, 0, None)))
    u.SendInput(3, seq, ctypes.sizeof(INPUT))
    u.SetCursorPos(was.x, was.y)


VK = {"enter": 0x0D, "escape": 0x1B, "tab": 0x09, "backspace": 0x08, "space": 0x20, "arrowdown": 0x28,
      "arrowup": 0x26, "arrowleft": 0x25, "arrowright": 0x27, "pagedown": 0x22, "pageup": 0x21, "delete": 0x2E}
