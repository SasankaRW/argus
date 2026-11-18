"""The Windows clipboard for clip sync, with ctypes only (no dependency).

A hidden message-only window (HWND_MESSAGE) on its own thread is registered with AddClipboardFormatListener and wakes on
WM_CLIPBOARDUPDATE; between copies that thread just waits in GetMessage (no polling). Reads open the clipboard with
3 retries 50 ms apart (another app may hold it). The exclusion formats are checked before the text is read.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
import time
from collections.abc import Callable
from ctypes import wintypes

from .clip import Flags

log = logging.getLogger("argus.send")

WM_CLOSE, WM_DESTROY, WM_CLIPBOARDUPDATE = 0x0010, 0x0002, 0x031D
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
HWND_MESSAGE = -3
RETRIES, RETRY_WAIT = 3, 0.05
BIGGEST = 1 << 20  # a clipboard text above 1 MB is not even read (the limit for sending is 256 KB)
LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HANDLE),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HANDLE), ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR)]


def _api():
    u = ctypes.WinDLL("user32", use_last_error=True)
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    H, U = wintypes.HWND, wintypes.UINT
    sig = [
        (u.DefWindowProcW, LRESULT, [H, U, wintypes.WPARAM, wintypes.LPARAM]),
        (u.RegisterClassW, wintypes.ATOM, [ctypes.POINTER(WNDCLASSW)]),
        (u.CreateWindowExW, H, [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int, H, wintypes.HMENU, wintypes.HINSTANCE,
                                wintypes.LPVOID]),
        (u.DestroyWindow, wintypes.BOOL, [H]),
        (u.GetMessageW, ctypes.c_int, [ctypes.POINTER(wintypes.MSG), H, U, U]),
        (u.DispatchMessageW, LRESULT, [ctypes.POINTER(wintypes.MSG)]),
        (u.PostMessageW, wintypes.BOOL, [H, U, wintypes.WPARAM, wintypes.LPARAM]),
        (u.PostQuitMessage, None, [ctypes.c_int]),
        (u.AddClipboardFormatListener, wintypes.BOOL, [H]),
        (u.RemoveClipboardFormatListener, wintypes.BOOL, [H]),
        (u.OpenClipboard, wintypes.BOOL, [H]),
        (u.CloseClipboard, wintypes.BOOL, []),
        (u.EmptyClipboard, wintypes.BOOL, []),
        (u.GetClipboardData, wintypes.HANDLE, [U]),
        (u.SetClipboardData, wintypes.HANDLE, [U, wintypes.HANDLE]),
        (u.IsClipboardFormatAvailable, wintypes.BOOL, [U]),
        (u.RegisterClipboardFormatW, U, [wintypes.LPCWSTR]),
        (k.GetModuleHandleW, wintypes.HMODULE, [wintypes.LPCWSTR]),
        (k.GlobalAlloc, wintypes.HGLOBAL, [U, ctypes.c_size_t]),
        (k.GlobalFree, wintypes.HGLOBAL, [wintypes.HGLOBAL]),
        (k.GlobalLock, wintypes.LPVOID, [wintypes.HGLOBAL]),
        (k.GlobalUnlock, wintypes.BOOL, [wintypes.HGLOBAL]),
        (k.GlobalSize, ctypes.c_size_t, [wintypes.HGLOBAL]),
    ]
    for fn, res, args in sig:
        fn.restype, fn.argtypes = res, args
    return u, k


class WinClipboard:
    """clip.ClipSource for Windows. Create it only on win32."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise OSError("the Windows clipboard is not available here")
        self.u, self.k = _api()
        self._thread: threading.Thread | None = None
        self._hwnd: int | None = None
        self._on_update: Callable[[], None] = lambda: None
        self._proc = WNDPROC(self._wndproc)  # kept alive: Windows calls it
        self._fmt: dict[str, int] = {}

    def fmt(self, name: str) -> int:
        if name not in self._fmt:
            self._fmt[name] = self.u.RegisterClipboardFormatW(name)
        return self._fmt[name]

    # -------------------------------------------------- the listener thread
    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_CLIPBOARDUPDATE:
            try:
                self._on_update()
            except Exception:  # noqa: BLE001  nothing may escape into Windows
                log.debug("clipboard update hook failed", exc_info=True)
            return 0
        if msg == WM_CLOSE:
            self.u.RemoveClipboardFormatListener(hwnd)
            self.u.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            self.u.PostQuitMessage(0)
            return 0
        return self.u.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _run(self, ready: threading.Event) -> None:
        cls = WNDCLASSW()
        cls.lpfnWndProc = self._proc
        cls.hInstance = self.k.GetModuleHandleW(None)
        cls.lpszClassName = f"ArgusClipListener{id(self)}"
        self.u.RegisterClassW(ctypes.byref(cls))
        hwnd = self.u.CreateWindowExW(0, cls.lpszClassName, "Argus clip", 0, 0, 0, 0, 0, HWND_MESSAGE, None,
                                      cls.hInstance, None)
        if not hwnd or not self.u.AddClipboardFormatListener(hwnd):
            log.warning("clipboard sync could not start listening (error %s)", ctypes.get_last_error())
            ready.set()
            if hwnd:
                self.u.DestroyWindow(hwnd)
            return
        self._hwnd = hwnd
        ready.set()
        msg = wintypes.MSG()
        while self.u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:  # blocked here between copies
            self.u.DispatchMessageW(ctypes.byref(msg))
        self._hwnd = None

    def start(self, on_update: Callable[[], None]) -> None:
        self._on_update = on_update
        if self._thread and self._thread.is_alive():
            return
        ready = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(ready,), name="clip-listener", daemon=True)
        self._thread.start()
        ready.wait(5)

    def stop(self) -> None:
        t, hwnd = self._thread, self._hwnd
        if hwnd:
            self.u.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        if t:
            t.join(2)
        self._thread = None

    # -------------------------------------------------- reading
    def _open(self, owner=None) -> bool:
        for i in range(RETRIES + 1):
            if self.u.OpenClipboard(owner):
                return True
            if i < RETRIES:
                time.sleep(RETRY_WAIT)
        return False

    def _dword(self, name: str) -> int | None:
        fmt = self.fmt(name)
        if not self.u.IsClipboardFormatAvailable(fmt):
            return None
        h = self.u.GetClipboardData(fmt)
        p = self.k.GlobalLock(h) if h else None
        if not p:
            return None
        try:
            return ctypes.c_uint32.from_address(p).value if self.k.GlobalSize(h) >= 4 else None
        finally:
            self.k.GlobalUnlock(h)

    def flags(self) -> Flags | None:
        if not self._open():
            return None
        try:
            return Flags(exclude=bool(self.u.IsClipboardFormatAvailable(
                             self.fmt("ExcludeClipboardContentFromMonitorProcessing"))),
                         history=self._dword("CanIncludeInClipboardHistory"),
                         cloud=self._dword("CanUploadToCloudClipboard"))
        finally:
            self.u.CloseClipboard()

    def text(self) -> str | None:
        if not self._open():
            return None
        try:
            if not self.u.IsClipboardFormatAvailable(CF_UNICODETEXT):
                return None
            h = self.u.GetClipboardData(CF_UNICODETEXT)
            if not h or self.k.GlobalSize(h) > BIGGEST:
                return None
            p = self.k.GlobalLock(h)
            if not p:
                return None
            try:
                return ctypes.wstring_at(p)
            finally:
                self.k.GlobalUnlock(h)
        finally:
            self.u.CloseClipboard()

    # -------------------------------------------------- writing
    def _alloc(self, data: bytes) -> int:
        h = self.k.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not h:
            raise OSError("out of memory")
        p = self.k.GlobalLock(h)
        ctypes.memmove(p, data, len(data))
        self.k.GlobalUnlock(h)
        return h

    def write(self, text: str, exclude: bool = True) -> None:
        """Put text on the clipboard. With `exclude`, Windows clipboard history and cloud sync skip it. OSError if the
        clipboard stays busy."""
        owner = self.u.CreateWindowExW(0, "STATIC", "", 0, 0, 0, 0, 0, HWND_MESSAGE, None, None, None)
        try:
            if not self._open(owner):  # an owner window is needed, or SetClipboardData fails after EmptyClipboard
                raise OSError("the clipboard is busy")
            try:
                self.u.EmptyClipboard()
                h = self._alloc((text + "\0").encode("utf-16-le"))
                if not self.u.SetClipboardData(CF_UNICODETEXT, h):
                    self.k.GlobalFree(h)
                    raise OSError("could not set the clipboard text")
                if exclude:
                    for name in ("ExcludeClipboardContentFromMonitorProcessing",):
                        hx = self._alloc(b"\x00\x00\x00\x00")
                        if not self.u.SetClipboardData(self.fmt(name), hx):
                            self.k.GlobalFree(hx)
            finally:
                self.u.CloseClipboard()
        finally:
            if owner:
                self.u.DestroyWindow(owner)
