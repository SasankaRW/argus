"""Ari's popup on the PC: a glowing pill over every app while Ari listens, thinks, works or talks (like Siri's).

    python -m argus.ari_popup            (dev.ps1 up starts it when ari.popup is on; needs: pip install -e .[popup])

It is a small see-through, always-on-top window at the top middle of the main screen that never takes the
keyboard. Inside it is Helios's Ari pill (/helios/popup.html), which follows Ari through the event stream: "Hey Ari"
on this PC, a question typed in Helios on the phone, a tool running, the answer. The page's title says when to
appear ("ari:open"), hide ("ari:idle") or open Helios on the Ari page ("ari:helios", you clicked it).
While it runs, Helios in a browser on this PC leaves the pill to it (no double pill).
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.parse
from pathlib import Path

W, H = 640, 150  # room for the pill and its glow all round (the pill sits 26 px down)
HIDE_AFTER_MS = 520  # let the pill's fold-away animation play before the window goes


def popup_url(base: str, token: str | None) -> str:
    q = f"?{urllib.parse.urlencode({'token': token})}" if token else ""
    return f"{base.rstrip('/')}/helios/popup.html{q}"


def placement(x: int, y: int, width: int) -> tuple[int, int, int, int]:
    """Top middle of the screen area (x, y, width of the available area)."""
    return x + (width - W) // 2, y, W, H


def action_for(title: str) -> str | None:
    return {"ari:open": "show", "ari:idle": "hide", "ari:helios": "helios"}.get(title)


def main(argv: list[str] | None = None) -> int:
    from .config import parse_env_file

    p = argparse.ArgumentParser(prog="ari-popup", description="Ari's popup over the whole screen")
    p.add_argument("--url", default=os.environ.get("ARGUS_URL", "http://127.0.0.1:8600"))
    args = p.parse_args(argv)
    token = os.environ.get("ARGUS_WORKER_TOKEN") or parse_env_file(Path(".env")).get("ARGUS_WORKER_TOKEN")
    try:
        from PySide6.QtCore import Qt, QTimer, QUrl  # type: ignore[import-not-found]
        from PySide6.QtGui import QDesktopServices  # type: ignore[import-not-found]
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
        g = app.primaryScreen().availableGeometry()
        view.setGeometry(*placement(g.x(), g.y(), g.width()))

    place()
    app.primaryScreen().availableGeometryChanged.connect(lambda _g: place())
    hide = QTimer(singleShot=True, interval=HIDE_AFTER_MS)
    hide.timeout.connect(view.hide)

    def on_title(title: str) -> None:
        what = action_for(title)
        if what == "show":
            hide.stop()
            if not view.isVisible():
                place()
                view.show()
            view.raise_()
        elif what == "hide":
            hide.start()
        elif what == "helios":
            QDesktopServices.openUrl(QUrl(f"{args.url.rstrip('/')}/helios/#ari"))

    view.titleChanged.connect(on_title)
    # Shown once so the page loads and starts listening, then hidden until Ari has something to show.
    view.setWindowOpacity(0.0)
    view.show()
    view.load(QUrl(popup_url(args.url, token)))
    QTimer.singleShot(1500, lambda: (view.hide(), view.setWindowOpacity(1.0)))
    print("Ari's popup is on (it shows while Ari listens, thinks or talks).", flush=True)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
