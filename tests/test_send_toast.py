"""The "Argus" notification: one per transfer, a bar that fills in place, then the result. Pure parts everywhere;
the Windows parts only on Windows."""

from __future__ import annotations

import os
import sys

import pytest

from argus.send import toast
from argus.send.progress import Progress
from argus.send.toast import Toaster, progress_values, progress_xml, result_xml


def test_progress_toast_binds_its_values():
    x = progress_xml("Receiving from my phone", "Home Lab review.pdf", "argus-send://cancel/abc")
    assert '<progress value="{progressValue}"' in x and 'status="{progressStatus}"' in x
    assert 'valueStringOverride="{progressValueString}"' in x and "Home Lab review.pdf" in x
    assert 'arguments="argus-send://cancel/abc"' in x and 'activationType="protocol"' in x


def test_result_toast_escapes_and_launches_by_protocol():
    x = result_xml("Got it from my phone", "a<b>.pdf", [("Open", "file:///C:/x/a.pdf")])
    assert "a&lt;b&gt;.pdf" in x and 'activationType="protocol"' in x and 'arguments="file:///C:/x/a.pdf"' in x
    assert "<actions>" not in result_xml("Cancelled", "a")


def test_values_show_percent_speed_and_time_left():
    p = Progress("o1", "send", "pc", "phone", "a", 1, 100_000_000, now=0.0)
    for i in range(1, 9):
        p.update(4_100_000 * i // 2, now=0.5 * i)
    v = progress_values(p)
    assert v["progressValueString"] == "16%" and v["progressValue"] == "0.16"
    assert "MB/s" in v["progressStatus"] and "left" in v["progressStatus"]


def test_one_notification_per_transfer_then_its_result(monkeypatch):
    posts, updates = [], []
    t = Toaster("pc", {"phone": "my phone"}.get)
    monkeypatch.setattr(t, "_post", lambda xml, tag, values=None: posts.append((xml, tag)))
    monkeypatch.setattr(t, "_update", lambda tag, values, seq: updates.append((tag, seq)))
    p = Progress("o1", "send", "pc", "phone", "a.bin", 1, 100, now=0.0)
    t.show(p)
    p.update(50, now=1.0)
    t.show(p)
    p.update(100, now=2.0, state="done")
    t.show(p)
    assert [tag for _, tag in posts] == ["o1", "o1"] and updates == [("o1", 2)]  # shown, updated, replaced
    assert "Sending to my phone" in posts[0][0] and "Sent to my phone" in posts[1][0]
    incoming = Progress("o2", "send", "phone", "pc", "b.pdf", 1, 10, now=0.0)
    incoming.update(10, now=0.1, state="done")
    t.show(incoming)  # received() shows the result, with the file to open
    assert len(posts) == 2


def test_tiny_file_shows_only_its_result(monkeypatch):
    posts = []
    t = Toaster("pc", str)
    monkeypatch.setattr(t, "_post", lambda xml, tag, values=None: posts.append(xml))
    p = Progress("o3", "send", "pc", "phone", "a", 1, 5, now=0.0)
    p.update(5, now=0.01, state="done")
    t.show(p)
    assert len(posts) == 1 and "Sent to phone" in posts[0]


@pytest.mark.skipif(sys.platform != "win32" or not os.environ.get("ARGUS_REAL_TOAST"),
                    reason="shows a real notification: run with ARGUS_REAL_TOAST=1 on Windows")
def test_registration_and_a_real_toast_dont_raise(monkeypatch):
    monkeypatch.delenv("ARGUS_NO_TOAST", raising=False)
    assert toast.register_app(app_id="Argus.SendTest")
    try:
        t = Toaster("pc", str, app_id="Argus.SendTest")
        p = Progress("test-toast", "send", "pc", "phone", "test.bin", 1, 100, now=0.0)
        t.show(p)
        p.update(100, now=1.0, state="cancelled")
        t.show(p)
        assert not t._failed
    finally:
        toast.unregister_app("Argus.SendTest")
