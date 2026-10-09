"""Ari's Workstation: Ari's own Windows virtual desktop (P7 of the Ari plan, part 1).

Ari opens what it works on there, so it never covers your windows or takes your keyboard: a search, a web page,
an app. You can look any time (Win+Ctrl+Right, or Task View), and drag windows between the desktops, since it's one
Windows session. "Move that window to me" brings Ari's window to the desktop you're on now, in front; "take this"
moves yours to the Workstation.

How a task is done, in order (the plan's ladder; this part has the direct routes):
  1. the direct route: a search address, a web page, an app's Start-menu entry;
  2-6. browser control, UI Automation, window messages, vision with numbered marks, the real mouse (later parts).
After each step Ari checks the result (the window is there, on the Workstation, with the page it asked for).

Never: windows in `never_touch` (the office's DirectFN, password managers) are not moved or worked in. Ari's
browser is its own (its own profile, under %LOCALAPPDATA%\\Argus\\browser), not yours.

The virtual desktops come from `pyvda` (pip install -e .[plugins]); windows through the Windows API.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from argus.worker import Context, PermanentError, workflow

PLUGIN = "workstation"
NAME = "Ari's Workstation"
WIN = platform.system() == "Windows"


@dataclass
class Win:
    hwnd: int
    title: str
    exe: str  # program name, lower case, without .exe ("chrome", "spotify")
    pid: int = 0


class Desk(Protocol):
    """What Ari needs from Windows (a fake one in the tests)."""

    def windows(self) -> list[Win]: ...
    def foreground(self) -> int: ...
    def current(self) -> str: ...                  # the desktop you're on (an id)
    def workstation(self) -> str: ...              # Ari's desktop, made and named if missing
    def desktop_of(self, hwnd: int) -> str | None: ...
    def move(self, hwnd: int, desk: str) -> None: ...
    def show_quietly(self, hwnd: int) -> None: ...  # shown without taking the keyboard
    def bring_front(self, hwnd: int) -> None: ...
    def launch(self, argv: list[str]) -> None: ...
    def go(self, desk: str) -> None: ...             # show that desktop (only for the real mouse)


# ------------------------------------------------------------------ the logic (no Windows calls)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", s.lower()).strip()


def untouchable(w: Win, never: list[str]) -> bool:
    hay = f"{w.exe} {w.title}".lower()
    return any(n.strip().lower() in hay for n in never if n.strip())


# what people call an app -> its program's name
ALIASES = {"vs code": "code", "vscode": "code", "visual studio code": "code", "edge": "msedge", "word": "winword",
           "excel": "excel", "file explorer": "explorer", "files": "explorer", "teams": "ms teams",
           "terminal": "windowsterminal", "settings": "systemsettings", "whatsapp": "whatsapp"}


def is_argus(w: Win) -> bool:
    """Argus's own windows (the island over the screen, the voice): never moved."""
    exe = w.exe.lower().removesuffix(".exe")
    return exe in ("python", "pythonw") and w.title.strip().lower() in ("ari", "")


def find(windows: list[Win], name: str, never: list[str]) -> Win | None:
    """The window you meant: its program, then all the words in program + title."""
    want = ALIASES.get(_norm(name), _norm(name))
    if not want:
        return None
    ok = [w for w in windows if not untouchable(w, never) and not is_argus(w)]
    for w in ok:
        if w.exe == want or _norm(w.exe) == want:
            return w
    words = want.split()
    for w in ok:
        hay = _norm(f"{w.exe} {w.title}")
        if all(x in hay for x in words):
            return w
    return None


def open_there(desk: Desk, argv: list[str], *, prefer: str | None, never: list[str], wait: float = 10.0,
               clock: Callable[[], float] | None = None, sleep: Callable[[float], None] | None = None) -> Win | None:
    """Start something and put its new window on the Workstation, without taking your keyboard. The new window is
    found by comparing the windows before and after (`prefer`: its program, when known)."""
    clock, sleep = clock or time.monotonic, sleep or time.sleep
    before = {w.hwnd for w in desk.windows()}
    yours = desk.foreground()
    ws = desk.workstation()
    desk.launch(argv)
    end = clock() + wait
    while clock() < end:
        new = [w for w in desk.windows() if w.hwnd not in before and w.title and not untouchable(w, never)]
        if prefer:
            new.sort(key=lambda w: w.exe != prefer)
        if new:
            w = new[0]
            desk.move(w.hwnd, ws)
            desk.show_quietly(w.hwnd)
            if yours and desk.foreground() != yours:
                desk.bring_front(yours)  # the new window grabbed the keyboard: give it back
            return w
        sleep(0.25)
    return None


def settled(desk: Desk, hwnd: int, want: list[str], *, wait: float = 8.0,
            clock: Callable[[], float] | None = None, sleep: Callable[[float], None] | None = None) -> dict:
    """The result check: the window is still there, on the Workstation, and its title shows what was asked."""
    clock, sleep = clock or time.monotonic, sleep or time.sleep
    ws = desk.workstation()
    end = clock() + wait
    title = ""
    while True:
        w = next((x for x in desk.windows() if x.hwnd == hwnd), None)
        if w is None:
            return {"ok": False, "why": "the window closed"}
        title = w.title
        there = desk.desktop_of(hwnd) == ws
        shows = all(x in _norm(title) for x in want)
        if there and shows:
            return {"ok": True, "title": title}
        if clock() >= end:
            return {"ok": False, "title": title,
                    "why": "it isn't on the Workstation" if not there else f"the page shows {title!r}"}
        sleep(0.5)


def search_words(query: str) -> list[str]:
    """Words of the search that a results page's title will show (the first three that aren't tiny)."""
    return [w for w in _norm(query).split() if len(w) > 2][:3]


def page_url(raw: str) -> str:
    raw = raw.strip()
    if re.match(r"^https?://", raw, re.I):
        return raw
    if re.fullmatch(r"[\w.-]+\.[a-z]{2,}(/\S*)?", raw, re.I):
        return "https://" + raw
    raise PermanentError(f"{raw!r} isn't a web address")


def profile_dir() -> str:
    """Ari's own browser profile: your browser, tabs and logins are left alone."""
    return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "Argus", "browser")


def browser_argv(exe: str, url: str) -> list[str]:
    """Ari's own browser without Playwright: its own profile, a new window."""
    return [exe, f"--user-data-dir={profile_dir()}", "--no-first-run", "--no-default-browser-check", "--new-window",
            url]


def ari_pids(profile: str) -> set[int]:  # pragma: no cover - needs the processes
    """The processes of Ari's browser (started with Ari's profile), so only its windows are moved."""
    import psutil

    out: set[int] = set()
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            if (p.info["name"] or "").lower() in ("chrome.exe", "msedge.exe", "chrome", "msedge") and any(
                    profile.lower() in str(a).lower() for a in p.info["cmdline"] or []):
                out.add(p.pid)
        except Exception:  # noqa: BLE001 - gone meanwhile
            continue
    return out


class Placer:
    """Keeps Ari's browser windows on the Workstation (a new one opens where you are) and your keyboard yours:
    called before and after everything Ari does in its browser."""

    def __init__(self, desk: Desk, pids: Callable[[], set[int]]):
        self.desk, self.pids, self.yours = desk, pids, 0

    def __call__(self) -> Win | None:
        d = self.desk
        pids = self.pids()
        mine = [w for w in d.windows() if w.pid in pids]
        fg = d.foreground()
        if fg and not any(w.hwnd == fg for w in mine):
            self.yours = fg  # what you were in
        ws = d.workstation()
        for w in mine:
            if d.desktop_of(w.hwnd) != ws:
                d.move(w.hwnd, ws)
                d.show_quietly(w.hwnd)
        if self.yours and any(w.hwnd == fg for w in mine):  # Ari's browser grabbed the keyboard: hand it back
            d.bring_front(self.yours)
        return mine[0] if mine else None


# ------------------------------------------------------------------ Windows


BROWSERS = {"chrome": ("chrome.exe", [r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
                                      r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
                                      r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"]),
            "edge": ("msedge.exe", [r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
                                    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"])}


def browser_exe(which: str) -> str:
    for name in (which, "edge"):  # Edge is always there on Windows
        exe, places = BROWSERS[name]
        for p in places:
            full = os.path.expandvars(p)
            if os.path.isfile(full):
                return full
        if shutil.which(exe):
            return str(shutil.which(exe))
    raise PermanentError("no Chrome or Edge found on this PC")


class WindowsDesk:  # pragma: no cover - Windows only
    """The real thing: pyvda for the virtual desktops, user32 for windows."""

    def __init__(self, state: dict):
        if not WIN:
            raise PermanentError("Ari's Workstation works on Windows only")
        try:
            import pyvda  # noqa: F401
        except ImportError:
            raise PermanentError("Ari's Workstation needs pyvda: pip install -e .[plugins]") from None
        import ctypes

        self.state = state  # {"id": the Workstation's desktop id} (kept in the plugin's store)
        self.u = ctypes.windll.user32
        self.ct = ctypes

    def _desktops(self):
        from pyvda import get_virtual_desktops

        return get_virtual_desktops()

    @staticmethod
    def _id(d) -> str:
        return str(getattr(d, "id", "") or getattr(d, "number", ""))

    def windows(self) -> list[Win]:
        import psutil

        ct, u = self.ct, self.u
        out: list[Win] = []
        proto = ct.WINFUNCTYPE(ct.c_bool, ct.c_void_p, ct.c_void_p)

        def each(h, _):
            if not u.IsWindowVisible(h) or u.GetWindow(h, 4):  # visible, no owner (a real app window)
                return True
            if u.GetWindowLongW(h, -20) & 0x80:  # WS_EX_TOOLWINDOW
                return True
            n = u.GetWindowTextLengthW(h)
            buf = ct.create_unicode_buffer(n + 1)
            u.GetWindowTextW(h, buf, n + 1)
            pid = ct.c_ulong()
            u.GetWindowThreadProcessId(h, ct.byref(pid))
            try:
                exe = psutil.Process(pid.value).name().lower().removesuffix(".exe")
            except Exception:  # noqa: BLE001
                exe = ""
            if buf.value:
                out.append(Win(int(h), buf.value, exe, int(pid.value)))
            return True

        u.EnumWindows(proto(each), 0)
        return out

    def foreground(self) -> int:
        return int(self.u.GetForegroundWindow() or 0)

    def current(self) -> str:
        from pyvda import VirtualDesktop

        return self._id(VirtualDesktop.current())

    def workstation(self) -> str:
        from pyvda import VirtualDesktop

        ds = self._desktops()
        for d in ds:  # Windows 11: by its name
            try:
                if getattr(d, "name", "") == NAME:
                    self.state["id"] = self._id(d)
                    return self.state["id"]
            except Exception:  # noqa: BLE001 - Windows 10 has no names
                pass
        for d in ds:  # by the id kept from last time
            if self._id(d) == self.state.get("id"):
                return self.state["id"]
        d = VirtualDesktop.create()
        try:
            d.rename(NAME)
        except Exception:  # noqa: BLE001 - Windows 10
            pass
        self.state["id"] = self._id(d)
        return self.state["id"]

    def desktop_of(self, hwnd: int) -> str | None:
        from pyvda import AppView

        try:
            return self._id(AppView(hwnd).desktop)
        except Exception:  # noqa: BLE001
            return None

    def move(self, hwnd: int, desk: str) -> None:
        from pyvda import AppView

        target = next(d for d in self._desktops() if self._id(d) == desk)
        AppView(hwnd).move(target)

    def show_quietly(self, hwnd: int) -> None:
        self.u.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE

    def bring_front(self, hwnd: int) -> None:
        u = self.u
        if u.IsIconic(hwnd):
            u.ShowWindow(hwnd, 9)  # SW_RESTORE
        fg = u.GetForegroundWindow()
        mine, theirs = self.ct.windll.kernel32.GetCurrentThreadId(), u.GetWindowThreadProcessId(fg, None)
        u.AttachThreadInput(mine, theirs, True)  # Windows lets the window in front hand over the keyboard
        try:
            u.BringWindowToTop(hwnd)
            u.SetForegroundWindow(hwnd)
        finally:
            u.AttachThreadInput(mine, theirs, False)

    def launch(self, argv: list[str]) -> None:
        subprocess.Popen(argv, close_fds=True)

    def go(self, desk: str) -> None:
        target = next(d for d in self._desktops() if self._id(d) == desk)
        target.go()


def start_menu_app(name: str) -> dict | None:  # pragma: no cover - Windows only
    """{name, id} of an installed app, matched like the Start menu (Get-StartApps)."""
    import difflib
    import json

    p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                        "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-StartApps | Select-Object Name, AppID"
                        " | ConvertTo-Json -Compress"], capture_output=True, text=True, timeout=20, encoding="utf-8",
                       errors="replace")
    rows = json.loads(p.stdout or "[]")
    rows = rows if isinstance(rows, list) else [rows]
    names = {_norm(r["Name"]): {"name": r["Name"], "id": r["AppID"]} for r in rows if r.get("Name")}
    want = _norm(name)
    if want in names:
        return names[want]
    starts = sorted((n for n in names if n.startswith(want)), key=len)
    if starts:
        return names[starts[0]]
    close = difflib.get_close_matches(want, list(names), n=1, cutoff=0.75)
    return names[close[0]] if close else None


# ------------------------------------------------------------------ workflows


def desk_for(ctx: Context) -> Desk:
    made = getattr(ctx, "desk", None)  # the tests give a fake one
    if made is not None:
        return made
    state = dict(ctx.store.get("workstation", {}) or {}) if ctx.store is not None else {}
    d = WindowsDesk(state)

    def keep() -> None:
        if ctx.store is not None and state:
            ctx.store.set("workstation", state)

    d.keep = keep  # type: ignore[attr-defined]
    return d


def _never(ctx: Context) -> list[str]:
    v = ctx.config.get("never_touch")
    return [str(x) for x in (v if isinstance(v, list) else str(v or "").split(","))]


def _remember(ctx: Context, desk: Desk, w: Win) -> None:
    getattr(desk, "keep", lambda: None)()
    if getattr(ctx, "store", None) is not None:
        ctx.store.set("last", {"hwnd": w.hwnd, "title": w.title, "exe": w.exe})


def have_playwright() -> bool:
    import importlib.util

    return importlib.util.find_spec("playwright") is not None


def browser_for(ctx: Context, desk: Desk) -> Any:
    """Ari's browser (Playwright), its windows kept on the Workstation."""
    made = getattr(ctx, "browser", None)  # the tests give a fake one
    if made is not None:
        return made
    from argus.worker.browser import AriBrowser

    profile = profile_dir()
    channel = "msedge" if ctx.config.get("browser") == "edge" else "chrome"
    place = Placer(desk, lambda: ari_pids(profile))
    place()  # notes the window you're in before Ari's browser shows up
    return AriBrowser.get(profile, channel, place=place)


def _browser_window(ctx: Context, desk: Desk) -> Win | None:
    pids = getattr(ctx, "browser_pids", None)
    w = Placer(desk, (lambda: pids) if pids is not None else (lambda: ari_pids(profile_dir())))()
    if w is not None:
        _remember(ctx, desk, w)
    return w


def _open_in_browser(ctx: Context, url: str, want: list[str]) -> dict:
    desk = desk_for(ctx)
    if getattr(ctx, "browser", None) is not None or (WIN and have_playwright()):  # Ari's driven browser: a new tab
        b = browser_for(ctx, desk)
        got = b.goto(url, new_tab=True)
        w = _browser_window(ctx, desk)
        shows = all(x in _norm(got.get("title") or "") for x in want)
        return {"done": shows, "window": got.get("title") or "", "on": NAME,
                **({} if shows else {"problem": f"the page shows {got.get('title')!r}"}),
                **({} if w else {"note": "I couldn't find its window"})}
    exe = browser_exe(str(ctx.config.get("browser") or "chrome"))
    prefer = os.path.basename(exe).lower().removesuffix(".exe")
    w = open_there(desk, browser_argv(exe, url), prefer=prefer, never=_never(ctx))
    if w is None:
        return {"done": False, "problem": "the browser didn't open a window"}
    _remember(ctx, desk, w)
    check = settled(desk, w.hwnd, want)
    return {"done": check["ok"], "window": check.get("title") or w.title, "on": NAME,
            **({} if check["ok"] else {"problem": check["why"]})}


@workflow(PLUGIN, "search")
def search(ctx: Context):
    q = str(ctx.input.get("query") or "").strip()
    if not q:
        raise PermanentError("what should I search for?")
    url = str(ctx.config.get("search_url") or "https://www.google.com/search?q={q}").replace(
        "{q}", urllib.parse.quote_plus(q))
    if ctx.dry_run:
        return {"would_open": url, "on": NAME, "dry_run": True}
    return ctx.step("search", _open_in_browser, ctx, url, search_words(q))


@workflow(PLUGIN, "browse")
def browse(ctx: Context):
    url = page_url(str(ctx.input.get("url") or ""))
    if ctx.dry_run:
        return {"would_open": url, "on": NAME, "dry_run": True}
    return ctx.step("open", _open_in_browser, ctx, url, [])


def _hand_over(ctx: Context, desk: Desk, why: str) -> dict:
    """Your turn: Ari's browser window comes to your desktop, in front (a password, a sign-in, a card number)."""
    w = _browser_window(ctx, desk)
    if w is not None:
        desk.move(w.hwnd, desk.current())
        desk.bring_front(w.hwnd)
    return {"done": False, "handed_to_you": True, "problem": why}


@workflow(PLUGIN, "do")
def do_in_browser(ctx: Context):
    """A task on a website, in Ari's browser on the Workstation (browser control: Ari types and clicks in the page,
    never with your mouse or keyboard). Asks before sending, buying, deleting or posting; hands the window to you for
    passwords and card numbers; asks you for anything it doesn't know."""
    from argus.worker.browser import run_task

    goal = str(ctx.input.get("goal") or "").strip()
    if not goal:
        raise PermanentError("what should I do in the browser?")
    url = str(ctx.input.get("url") or "").strip()
    start = page_url(url) if url else None
    if ctx.dry_run:
        return {"would_do": goal, "on": NAME, "dry_run": True}
    desk = desk_for(ctx)
    out = run_task(ctx, browser_for(ctx, desk), goal, start_url=start, tag="task")
    w = _browser_window(ctx, desk)
    if out.get("handover"):
        return {**out, **ctx.step("hand over", _hand_over, ctx, desk, out["problem"])}
    return {**out, "on": NAME, **({"window": w.title} if w else {})}


def surface_for(ctx: Context, hwnd: int) -> Any:
    """A window's controls through UI Automation (a fake one in the tests)."""
    made = getattr(ctx, "surface_for", None)
    if made is not None:
        return made(hwnd)
    from argus.worker.uia import UiaSurface

    return UiaSurface(hwnd)


def mouse_around(desk: Desk, hwnd: int, sleep: Callable[[float], None] = time.sleep) -> Callable[[Callable], None]:
    """For the real mouse: show the Workstation with that window in front, do the one click, go back to the desktop
    you were on."""
    def around(click: Callable[[], None]) -> None:
        here, ws = desk.current(), desk.workstation()
        desk.go(ws)
        sleep(0.4)
        desk.bring_front(hwnd)
        sleep(0.2)
        try:
            click()
            sleep(0.3)
        finally:
            desk.go(here)

    return around


def pick_surface(ctx: Context, hwnd: int) -> Any:
    """The ladder for an app: its controls through UI Automation when it lists them (3 or more), else its picture
    with numbered boxes for the vision model (clicks as window messages)."""
    uia = surface_for(ctx, hwnd)
    try:
        enough = len(uia.snapshot()["items"]) >= 3
    except Exception:  # noqa: BLE001 - no UI Automation at all
        enough, uia = False, None
    if enough:
        return uia
    made = getattr(ctx, "vision_for", None)
    if made is not None:
        return made(hwnd, uia)
    from argus.worker.uia import VisionSurface

    return VisionSurface(hwnd, uia)


@workflow(PLUGIN, "in_app")
def in_app(ctx: Context):
    """A task in a desktop app on the Workstation, through its controls (UI Automation: no mouse, no focus)."""
    from argus.worker.browser import run_task

    app = str(ctx.input.get("app") or "").strip()
    goal = str(ctx.input.get("goal") or "").strip()
    if not app or not goal:
        raise PermanentError("which app, and what should I do in it?")
    never = _never(ctx)
    if any(n.strip() and n.strip().lower() in app.lower() for n in never):
        return {"done": False, "problem": f"I don't work in {app}"}
    if ctx.dry_run:
        return {"would_do": goal, "in": app, "dry_run": True}

    def window() -> dict:
        desk = desk_for(ctx)
        ws_id = desk.workstation()
        wins = desk.windows()
        w = find([x for x in wins if desk.desktop_of(x.hwnd) == ws_id], app, never)
        if w is None:  # not on the Workstation yet: open it there
            opened = open_app(SimpleCtx(ctx, {"name": app}))
            if not opened.get("done"):
                return {"hwnd": 0, "problem": opened.get("problem") or f"I couldn't open {app}"}
            w = find([x for x in desk.windows() if desk.desktop_of(x.hwnd) == ws_id], app, never)
        if w is None:
            return {"hwnd": 0, "problem": f"{app} has no window on my workstation"}
        _remember(ctx, desk, w)
        return {"hwnd": w.hwnd, "title": w.title}

    got = ctx.step("window", window)
    if not got["hwnd"]:
        return {"done": False, "problem": got["problem"]}
    surface = pick_surface(ctx, got["hwnd"])
    if hasattr(surface, "around"):
        surface.around = mouse_around(desk_for(ctx), got["hwnd"])
    out = run_task(ctx, surface, goal, tag="app",
                   allow=("click", "type", "select", "press", "scroll", "mouse", "done", "ask"))
    if out.get("handover"):
        desk = desk_for(ctx)
        desk.move(got["hwnd"], desk.current())
        desk.bring_front(got["hwnd"])
        return {**out, "handed_to_you": True}
    return {**out, "window": got["title"], "on": NAME}


@workflow(PLUGIN, "fill")
def fill(ctx: Context):
    """"Fill this form": the window you're looking at. Ari only types into fields and picks options (no buttons:
    you check and press Submit / Send yourself), asks for what it doesn't know, never types passwords or numbers."""
    from argus.worker.browser import FILL_ONLY, run_task

    goal = str(ctx.input.get("goal") or "").strip() or "Fill in this form with what you know; ask for the rest"
    if ctx.dry_run:
        return {"would_fill": "the window in front", "dry_run": True}

    def window() -> dict:
        desk = desk_for(ctx)
        name = str(ctx.input.get("window") or "").strip()
        w = _which(ctx, desk, name, "front")
        if w is None:
            why = "" if name else " (or it's one I never touch)"
            return {"hwnd": 0, "problem": "I can't work in that window" + why}
        return {"hwnd": w.hwnd, "title": w.title}

    got = ctx.step("window", window)
    if not got["hwnd"]:
        return {"done": False, "problem": got["problem"]}
    out = run_task(ctx, surface_for(ctx, got["hwnd"]), goal + " (fill fields only; the user presses the buttons)",
                   tag="fill", allow=FILL_ONLY)
    filled = int(out.get("steps") or 0)
    if out.get("done"):
        return {"done": True, "window": got["title"], "filled": filled,
                "answer": (out.get("answer") or "Filled it in.") + " Check it, then press the button yourself."}
    return {**out, "window": got["title"]}


class SimpleCtx:
    """The same job, another workflow's input (open_app inside in_app)."""

    def __init__(self, ctx: Context, inp: dict):
        self._ctx, self.input = ctx, inp

    def __getattr__(self, k: str) -> Any:
        return getattr(self._ctx, k)

    def step(self, name: str, fn: Any, *a: Any, **k: Any) -> Any:
        return fn(*a, **k)  # already inside the caller's step


# Spotify in Ari's browser (the web player): the direct route first, then the model, and what worked is a recipe.
SPOTIFY_ROW = '[data-testid="tracklist-row"]'


def spotify_now(page: Any) -> dict:  # pragma: no cover - a real page
    """The result check: is something playing, and what."""
    pp = page.locator('[data-testid="control-button-playpause"]').first
    label = (pp.get_attribute("aria-label") or "") if pp.count() else ""
    t = page.locator('[data-testid="context-item-info-title"]').first
    return {"ok": label.lower() == "pause", "playing": t.inner_text().strip() if t.count() else ""}


def spotify_first(page: Any) -> dict:  # pragma: no cover - a real page
    """Play the first track of the search results already open."""
    try:
        page.wait_for_selector(f'{SPOTIFY_ROW}, [data-testid="login-button"]', timeout=15000)
    except Exception:  # noqa: BLE001
        return {"ok": False, "why": "the results didn't load"}
    rows = page.locator(SPOTIFY_ROW)
    if rows.count() == 0:
        login = page.locator('[data-testid="login-button"]').count() > 0
        return {"ok": False, "login": login, "why": "Spotify wants you to sign in" if login else "no results"}
    row = rows.first
    row.hover()
    btn = row.locator('button[aria-label^="Play"]').first
    if btn.count():
        btn.click()
    else:
        row.dblclick()
    page.wait_for_timeout(2000)
    return spotify_now(page)


@workflow(PLUGIN, "spotify")
def spotify(ctx: Context):
    """"Play <song> on Spotify": Spotify's web player in Ari's browser (you sign in there once)."""
    from argus.worker.browser import run_task

    q = str(ctx.input.get("query") or "").strip()
    if not q:
        raise PermanentError("what should I play?")
    url = f"https://open.spotify.com/search/{urllib.parse.quote(q)}/tracks"
    if ctx.dry_run:
        return {"would_play": q, "dry_run": True}
    desk = desk_for(ctx)
    b = browser_for(ctx, desk)
    ctx.step("open", b.goto, url)
    got = ctx.step("play", b.run, getattr(ctx, "spotify_first", None) or spotify_first)  # the direct route
    if got.get("login"):
        return ctx.step("hand over", _hand_over, ctx, desk,
                        "Spotify wants you to sign in once, in this window (it's my browser): use your email and "
                        "password, or the email code; Google sign-in won't work here. Then ask me again")
    if not got.get("ok"):  # Spotify changed its page: work it out, and keep what worked as a recipe
        check = getattr(ctx, "spotify_now", None) or (lambda: b.run(spotify_now))
        out = run_task(ctx, b, f'Play the song "{q}" on Spotify (the web player is open)', q=q,
                       recipe_key="spotify.play", check=check, tag="spotify")
        got = ctx.step("check", check) if out.get("done") else {"ok": False, "why": out.get("problem")}
    _browser_window(ctx, desk)
    if not got.get("ok"):
        return {"done": False, "problem": f"I couldn't get {q} playing ({got.get('why') or 'not playing'})"}
    return {"done": True, "playing": got.get("playing") or q}


@workflow(PLUGIN, "open_app")
def open_app(ctx: Context):
    asked = str(ctx.input.get("name") or "").strip()
    if not asked:
        raise PermanentError("which app?")

    def go() -> dict:
        desk = desk_for(ctx)
        never = _never(ctx)
        if any(n.strip() and n.strip().lower() in asked.lower() for n in never):
            return {"done": False, "problem": f"I don't work in {asked}"}
        already = find(desk.windows(), asked, never)
        if already is not None and desk.desktop_of(already.hwnd) != desk.workstation():
            return {"done": False, "open_on_your_desktop": already.title,
                    "problem": f"{asked} is already open on your desktop: say \"take this\" to give it to me"}
        if already is not None:
            _remember(ctx, desk, already)
            return {"done": True, "window": already.title, "on": NAME, "note": "it was open there already"}
        app = (getattr(ctx, "find_app", None) or start_menu_app)(asked)
        if app is None:
            return {"done": False, "problem": f"I can't find an app called {asked!r}"}
        if ctx.dry_run:
            return {"would_open": app["name"], "on": NAME, "dry_run": True}
        w = open_there(desk, ["explorer.exe", f"shell:AppsFolder\\{app['id']}"], prefer=None, never=never,
                       wait=20)
        if w is None:
            return {"done": False, "problem": f"{app['name']} started but no window showed up"}
        _remember(ctx, desk, w)
        check = settled(desk, w.hwnd, [])
        return {"done": check["ok"], "window": w.title, "on": NAME, **({} if check["ok"] else
                                                                         {"problem": check["why"]})}

    return ctx.step("open", go)


def _which(ctx: Context, desk: Desk, name: str, default: str) -> Win | None:
    """The window meant: by name; else Ari's last one ("that") or the one in front ("this")."""
    wins = desk.windows()
    never = _never(ctx)
    if name:
        ws = desk.workstation()
        mine = [w for w in wins if desk.desktop_of(w.hwnd) == ws]
        return find(mine, name, never) or find(wins, name, never)
    if default == "last":
        last = (ctx.store.get("last", {}) if ctx.store is not None else {}) or {}
        w = next((x for x in wins if x.hwnd == last.get("hwnd")), None)
        return None if w is None or untouchable(w, never) else w
    fg = desk.foreground()
    w = next((x for x in wins if x.hwnd == fg), None)
    return None if w is None or untouchable(w, never) or is_argus(w) else w


@workflow(PLUGIN, "give")
def give(ctx: Context):
    """"Move that window to me": to the desktop you're on now, in front."""
    name = str(ctx.input.get("name") or "").strip()

    def go() -> dict:
        desk = desk_for(ctx)
        w = _which(ctx, desk, name, "last")
        if w is None:
            return {"done": False, "problem": f"I can't find {name or 'the window I was working in'}"}
        if ctx.dry_run:
            return {"would_move": w.title, "dry_run": True}
        here = desk.current()
        if desk.desktop_of(w.hwnd) != here:
            desk.move(w.hwnd, here)
        desk.bring_front(w.hwnd)
        ok = desk.desktop_of(w.hwnd) == here
        return {"done": ok, "window": w.title, **({} if ok else {"problem": "Windows wouldn't move it"})}

    return ctx.step("give", go)


@workflow(PLUGIN, "take")
def take(ctx: Context):
    """"Take this": your window (by name, else the one in front) goes to the Workstation."""
    name = str(ctx.input.get("name") or "").strip()

    def go() -> dict:
        desk = desk_for(ctx)
        w = _which(ctx, desk, name, "front")
        if w is None:
            return {"done": False, "problem": f"I can't take {name or 'that window'}" + (
                "" if name else " (or it's one I never touch)")}
        if ctx.dry_run:
            return {"would_take": w.title, "dry_run": True}
        ws = desk.workstation()
        desk.move(w.hwnd, ws)
        _remember(ctx, desk, w)
        ok = desk.desktop_of(w.hwnd) == ws
        return {"done": ok, "window": w.title, "on": NAME, **({} if ok else {"problem": "Windows wouldn't move it"})}

    return ctx.step("take", go)


@workflow(PLUGIN, "status")
def status(ctx: Context):
    def go() -> dict:
        desk = desk_for(ctx)
        ws = desk.workstation()
        getattr(desk, "keep", lambda: None)()
        return {"on": NAME, "windows": [w.title for w in desk.windows() if desk.desktop_of(w.hwnd) == ws
                                        and not untouchable(w, _never(ctx))]}

    return ctx.step("look", go)

