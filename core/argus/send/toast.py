"""The "Argus" notification on Windows for Argus Send: one per transfer, its progress bar filling in place, then the
result (Sent / Got it, with Open and Show in folder; Couldn't send; Cancelled).

Native Windows notifications through pywinrt, shown from the Send agent itself: no PowerShell and no console window.
The name "Argus" and its icon come from a per-user AppUserModelID registry key (no Start-menu app needed). The
Cancel button is an `argus-send://cancel/<id>` link handled by `pythonw -m argus.send cancel-url` (no window).
Everything here is best effort: a notification that can't be shown never touches a transfer.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from .progress import Progress

log = logging.getLogger("argus.send")
APP_ID = "Argus.Send"
GROUP = "send"
ROOT = Path(__file__).resolve().parents[3]
ICON = ROOT / "helios" / "public" / "icon-192.png"


def enabled() -> bool:
    return sys.platform == "win32" and not os.environ.get("ARGUS_NO_TOAST")


# ---------------------------------------------------------------------- registration (per user, idempotent)

def register_app(icon: Path = ICON, app_id: str = APP_ID) -> bool:
    """Name the notifications "Argus" with the Argus icon (HKCU\\Software\\Classes\\AppUserModelId\\<app_id>)."""
    if sys.platform != "win32":
        return False
    try:
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\AppUserModelId\{app_id}") as k:
            winreg.SetValueEx(k, "DisplayName", 0, winreg.REG_SZ, "Argus")
            if icon.exists():
                winreg.SetValueEx(k, "IconUri", 0, winreg.REG_SZ, str(icon))
        return True
    except OSError as e:
        log.debug("couldn't register the notification name: %s", e)
        return False


def unregister_app(app_id: str) -> None:
    if sys.platform == "win32":
        import winreg

        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\AppUserModelId\{app_id}")
        except OSError:
            pass


def register_cancel_protocol(pythonw: Path | None = None) -> bool:
    """argus-send://cancel/<id> runs `pythonw -m argus.send cancel-url <url>`: the notification's Cancel button."""
    if sys.platform != "win32":
        return False
    exe = pythonw or Path(sys.executable).with_name("pythonw.exe")
    try:
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\argus-send") as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, "URL:Argus Send")
            winreg.SetValueEx(k, "URL Protocol", 0, winreg.REG_SZ, "")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\argus-send\shell\open\command") as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, f'"{exe}" -m argus.send cancel-url "%1"')
        return True
    except OSError as e:
        log.debug("couldn't register the cancel link: %s", e)
        return False


# ---------------------------------------------------------------------- what the notifications say (pure)

def _size(n: float) -> str:
    for unit, k in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if n >= k:
            return f"{n / k:.1f} {unit}"
    return f"{int(n)} B"


def _left(s: float) -> str:
    return f"{int(s)} s left" if s < 60 else f"{int(s // 60)} min left"


def progress_values(p: Progress) -> dict[str, str]:
    frac = p.done / p.total if p.total else 1.0
    status = "Checking" if p.state == "verifying" else f"{_size(p.speed)}/s" if p.speed else "Starting"
    eta = p.eta()
    if eta is not None and p.state == "moving":
        status += f" · {_left(eta)}"
    return {"progressValue": f"{max(0.0, min(1.0, frac)):.2f}", "progressValueString": f"{round(frac * 100)}%",
            "progressStatus": status}


def _actions(buttons: list[tuple[str, str]]) -> str:
    if not buttons:
        return ""
    inner = "".join(f'<action content={quoteattr(c)} activationType="protocol" arguments={quoteattr(u)}/>'
                    for c, u in buttons)
    return f"<actions>{inner}</actions>"


def progress_xml(title: str, label: str, cancel_uri: str | None = None) -> str:
    buttons = [("Cancel", cancel_uri)] if cancel_uri else []
    return ('<toast><visual><binding template="ToastGeneric">'
            f"<text>{escape(title)}</text><text>{escape(label)}</text>"
            '<progress value="{progressValue}" valueStringOverride="{progressValueString}" '
            'status="{progressStatus}"/>'
            f"</binding></visual>{_actions(buttons)}</toast>")


def result_xml(title: str, body: str, buttons: list[tuple[str, str]] | None = None) -> str:
    return ('<toast><visual><binding template="ToastGeneric">'
            f"<text>{escape(title)}</text><text>{escape(body)}</text>"
            f"</binding></visual>{_actions(buttons or [])}</toast>")


# ---------------------------------------------------------------------- showing them

class Toaster:
    """Shows and updates the notifications. `name_of` turns a device id into its name ("my phone")."""

    def __init__(self, me: str = "", name_of: Callable[[str], str] = str, app_id: str = APP_ID) -> None:
        self.me, self.name_of, self.app_id = me, name_of, app_id
        self.shown: dict[str, int] = {}  # offer id -> update sequence number
        self._failed = False

    def _winrt(self):
        from winrt.windows.data.xml.dom import XmlDocument
        from winrt.windows.ui.notifications import NotificationData, ToastNotification, ToastNotificationManager
        return XmlDocument, NotificationData, ToastNotification, ToastNotificationManager

    def _post(self, xml: str, tag: str | None, values: dict[str, str] | None = None) -> None:
        if not enabled():
            return
        try:
            XmlDocument, NotificationData, ToastNotification, Manager = self._winrt()
            doc = XmlDocument()
            doc.load_xml(xml)
            t = ToastNotification(doc)
            if tag:
                t.tag, t.group = tag[:64], GROUP
            if values is not None:
                data = NotificationData()
                for k, v in values.items():
                    data.values.insert(k, v)
                data.sequence_number = 1
                t.data = data
            Manager.create_toast_notifier(self.app_id).show(t)
        except Exception as e:  # noqa: BLE001  a missing notification must never matter
            self._note(e)

    def _update(self, tag: str, values: dict[str, str], seq: int) -> None:
        if not enabled():
            return
        try:
            _, NotificationData, _, Manager = self._winrt()
            data = NotificationData()
            for k, v in values.items():
                data.values.insert(k, v)
            data.sequence_number = seq
            Manager.create_toast_notifier(self.app_id).update_with_tag_and_group(data, tag[:64], GROUP)
        except Exception as e:  # noqa: BLE001
            self._note(e)

    def _note(self, e: Exception) -> None:
        if not self._failed:
            self._failed = True
            log.warning("Send notifications are off: %s", e)

    def show(self, p: Progress) -> None:
        """A transfer moved on: show its progress (first time), update it, or turn it into the result."""
        incoming = p.dst == self.me
        peer = self.name_of(p.src if incoming else p.dst)
        if p.state in ("moving", "verifying"):
            seq = self.shown.get(p.offer_id)
            if seq is None:
                title = f"Receiving from {peer}" if incoming else f"Sending to {peer}"
                cancel = None if incoming else f"argus-send://cancel/{p.offer_id}"
                self.shown[p.offer_id] = 1
                self._post(progress_xml(title, p.label, cancel), p.offer_id, progress_values(p))
            else:
                self.shown[p.offer_id] = seq + 1
                self._update(p.offer_id, progress_values(p), seq + 1)
            return
        self.shown.pop(p.offer_id, None)
        if p.state == "done" and incoming:
            return  # received() shows the result, with the saved file to open
        if p.state == "done":
            self._post(result_xml(f"Sent to {peer}", p.label), p.offer_id)
        elif p.state == "cancelled":
            self._post(result_xml("Cancelled", f"{p.label} to {peer}"), p.offer_id)
        elif p.state == "failed":
            self._post(result_xml(f"Couldn't send {p.label} to {peer}", "It will try again when it can"), p.offer_id)

    def received(self, offer_id: str, peer: str, label: str, saved: list[str]) -> None:
        buttons = []
        if saved:
            first = Path(saved[0])
            if len(saved) == 1:
                buttons.append(("Open", first.as_uri()))
            buttons.append(("Show in folder", first.parent.as_uri()))
        self._post(result_xml(f"Got it from {peer}", label, buttons), offer_id)

    def link(self, clip_id: str, peer: str, url: str) -> None:
        """A link from the other device: Open (the default browser) and Copy (argus-send://copy/<url, base64>)."""
        from .clip import short_url
        from .keys import b64

        copy = "argus-send://copy/" + b64(url.encode("utf-8")).replace("+", "-").replace("/", "_")
        self._post(result_xml(f"Link from {peer}", short_url(url), [("Open", url), ("Copy", copy)]), clip_id)

    def problem(self, title: str, body: str) -> None:
        self._post(result_xml(title, body), None)
