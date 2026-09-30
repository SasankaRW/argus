"""Ari's island on the PC: a black shape grown out of the top edge of the main screen, over every app (like the
iPhone's Dynamic Island, but part of the screen's edge).

    python -m argus.ari_popup            (dev.ps1 up starts it when ari.popup is on; needs: pip install -e .[popup])

A see-through, always-on-top window at the top middle of the screen that never takes the keyboard. Inside is
Helios's island (/helios/popup.html), which follows Ari through the event stream: a slim lip while idle, wider while
Ari listens, thinks, works or talks, the answer when done, and details when you click it. The page's title tells
the window where the island is ("ari:<mode>|<w>x<h>": only that part takes clicks, the rest of the window lets
them through to the app below) and when to open Helios ("ari:go|<hash>").
While it runs, Helios in a browser on this PC leaves the pill to it (no double pill).
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.parse
from pathlib import Path

W, H = 640, 360  # room for the island at its biggest (details), its shoulders and shadow
SHRINK_AFTER_MS = 750  # the island folds in on a spring (~.6 s); the click area follows after that


def popup_url(base: str, token: str | None) -> str:
    q = f"?{urllib.parse.urlencode({'token': token})}" if token else ""
    return f"{base.rstrip('/')}/helios/popup.html{q}"


def placement(x: int, y: int, width: int) -> tuple[int, int, int, int]:
    """Top middle of the screen area (x, y, width of the available area)."""
    return x + (width - W) // 2, y, W, H


def parse_title(title: str) -> tuple[str, object] | None:
    """The page's title -> ("area", (w, h)) where the island is, or ("go", "#hash") to open Helios; else None."""
    if not title.startswith("ari:") or "|" not in title:
        return None
    what, _, arg = title[4:].partition("|")
    if what == "go":
        return ("go", arg)
    try:
        w, h = (int(x) for x in arg.split("x", 1))
    except ValueError:
        return None
    return ("area", (max(1, min(w, W)), max(1, min(h, H))))


def open_target(base: str, arg: str) -> str:
    """What "ari:go|<arg>" opens: a website shortcut (url:https://...) or a Helios page."""
    if arg.startswith("url:") and arg[4:].startswith(("http://", "https://")):
        return arg[4:]
    return f"{base.rstrip('/')}/helios/{'#' + arg if arg and not arg.startswith('url:') else ''}"


def click_area(w: int, h: int) -> tuple[int, int, int, int]:
    """The part of the window that takes clicks: the island, centred at the top (x, y, w, h in the window)."""
    return (W - w) // 2, 0, w, h


def main(argv: list[str] | None = None) -> int:
    from .config import parse_env_file

    p = argparse.ArgumentParser(prog="ari-popup", description="Ari's popup over the whole screen")
    p.add_argument("--url", default=os.environ.get("ARGUS_URL", "http://127.0.0.1:8600"))
    args = p.parse_args(argv)
    token = os.environ.get("ARGUS_WORKER_TOKEN") or parse_env_file(Path(".env")).get("ARGUS_WORKER_TOKEN")
    try:
        from PySide6.QtCore import Qt, QTimer, QUrl  # type: ignore[import-not-found]
        from PySide6.QtGui import QDesktopServices, QRegion  # type: ignore[import-not-found]
        from PySide6.QtWebEngineWidgets import QWebEngineView  # type: ignore[import-not-found]
        from PySide6.QtWidgets import QApplication  # type: ignore[import-not-found]
    except ImportError as e:
        print(f"ari-popup needs its extras: pip install -e .[popup]  ({e})", file=sys.stderr)
        return 2

    app = QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)  # hiding the window must not end the app
    view = QWebEngineView()
    view.setWindowTitle("Ari")
    view.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                        | Qt.WindowType.Tool | Qt.WindowType.WindowDoesNotAcceptFocus)
    view.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    view.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    view.setStyleSheet("background: transparent")
    view.page().setBackgroundColor(Qt.GlobalColor.transparent)

    def place() -> None:
        g = app.primaryScreen().geometry()  # the whole screen: the island touches its very top edge
        view.setGeometry(*placement(g.x(), g.y(), g.width()))

    place()
    app.primaryScreen().geometryChanged.connect(lambda _g: place())
    area = {"now": (220, 21), "next": (220, 21)}
    view.setMask(QRegion(*click_area(*area["now"])))
    shrink = QTimer(singleShot=True, interval=SHRINK_AFTER_MS)

    def apply_next() -> None:
        area["now"] = area["next"]
        view.setMask(QRegion(*click_area(*area["now"])))

    shrink.timeout.connect(apply_next)

    def on_title(title: str) -> None:
        got = parse_title(title)
        if got is None:
            return
        what, arg = got
        if what == "area":
            area["next"] = arg  # type: ignore[assignment]
            w, h = area["next"]
            if w >= area["now"][0] and h >= area["now"][1]:
                shrink.stop()  # growing: the whole new area at once, so the animation isn't cut off
                apply_next()
            else:
                shrink.start()  # shrinking: after the island's own animation
            view.raise_()
        elif what == "go":
            QDesktopServices.openUrl(QUrl(open_target(args.url, str(arg))))

    view.titleChanged.connect(on_title)
    view.load(QUrl(popup_url(args.url, token)))
    view.show()
    print("Ari's island is on (top of the screen).", flush=True)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
