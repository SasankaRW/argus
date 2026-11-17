"""A Windows notification for Argus Send (queued, sent, received, failed). Elsewhere it does nothing.

Shown through Windows PowerShell's WinRT toast API, started with CREATE_NO_WINDOW so no console ever appears. The
text travels in an environment variable, never on the command line, so file names can't break the script.
"""

from __future__ import annotations

import os
import subprocess
import sys
from xml.sax.saxutils import escape

APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"  # a registered app id
SCRIPT = (
    "[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime]|Out-Null;"
    "$x=[Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom.XmlDocument,ContentType=WindowsRuntime]::new();"
    "$x.LoadXml($env:ARGUS_TOAST_XML);"
    "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:ARGUS_TOAST_APP)"
    ".Show([Windows.UI.Notifications.ToastNotification]::new($x))"
)


def toast_xml(title: str, body: str = "") -> str:
    lines = f"<text>{escape(title)}</text>" + (f"<text>{escape(body)}</text>" if body else "")
    return f'<toast><visual><binding template="ToastGeneric">{lines}</binding></visual></toast>'


def toast(title: str, body: str = "") -> bool:
    """Show a notification; True when one was started. Never raises: a missing toast must not stop a send."""
    if sys.platform != "win32" or os.environ.get("ARGUS_NO_TOAST"):
        return False
    env = {**os.environ, "ARGUS_TOAST_XML": toast_xml(title, body), "ARGUS_TOAST_APP": APP_ID}
    try:
        subprocess.Popen(  # noqa: S603  fixed script, text passed through the environment
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", SCRIPT],
            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return True
    except OSError:
        return False


def toast_for_event(kind: str, peer: str | None, label: str | None, detail: str | None = None) -> bool:
    """The notification for a Send history event, or nothing for the quiet ones."""
    peer, label = peer or "a device", label or "files"
    words = {
        "sent": (f"Sent {label} to {peer}", detail or ""),
        "received": (f"Got {label} from {peer}", "Saved in Argus Shared"),
        "failed": (f"Couldn't send {label} to {peer}", detail or ""),
        "not_delivered": (f"{label} didn't reach {peer}", detail or ""),
    }.get(kind)
    return toast(*words) if words else False
