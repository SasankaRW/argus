"""The phone app says when Android won't show Argus's notifications (a channel switched off): Argus keeps the reason on
the phone app's box for Helios, clears it when the app says it's fine, and forgets it when the app goes quiet."""

from __future__ import annotations

import urllib.parse

from argus.outbox import PhoneInbox
from fakes import FakePhoneApp
from test_approvals import make_argus
from test_worker import Server, client

REASON = 'the "Approvals and warnings" notifications are off'


def test_the_inbox_remembers_why_android_is_blocking():
    t = [1000.0]
    box = PhoneInbox(clock=lambda: t[0])
    box.seen("pixel", blocked=REASON)
    assert box.blocked_now() == REASON
    box.seen("pixel")  # a poll that doesn't say keeps it
    assert box.blocked_now() == REASON
    box.seen("pixel", blocked="x" * 300)
    assert len(box.blocked_now()) == 120
    box.seen("pixel", blocked="")  # switched back on
    assert box.blocked_now() == ""


def test_an_old_reason_goes_with_the_connection():
    t = [1000.0]
    box = PhoneInbox(clock=lambda: t[0])
    box.seen("pixel", blocked=REASON)
    t[0] += 91  # the app hasn't asked for 90 s: offline, and what it said then no longer counts
    assert box.blocked_now() == ""


def test_the_map_shows_it_on_the_phone_app(tmp_path):
    argus = make_argus(tmp_path).open()
    with Server(argus) as srv:
        app = FakePhoneApp(srv.url)
        q = urllib.parse.quote(REASON)
        assert app._call("GET", f"/phone/inbox?device=pixel&blocked={q}")[0] == 200
        cl = client(srv.url, "tok")
        node = next(n for n in cl.get("/map")["nodes"] if n["id"] == "phone-app")
        assert node["blocked"] == REASON
        assert app._call("GET", "/phone/inbox?device=pixel&blocked=")[0] == 200
        node = next(n for n in cl.get("/map")["nodes"] if n["id"] == "phone-app")
        assert node["blocked"] == ""
