"""Live Send transfers on Argus: merged from both ends, kept briefly after finishing, cancel reaches the sender."""

from __future__ import annotations

from fastapi.testclient import TestClient

from argus.api import create_app
from argus.send_live import SendLive
from test_send_api import make


def snap(**over):
    return {"offer_id": "o" * 8, "leg": "send", "from": "phone", "to": "pc", "label": "a", "files": 1,
            "total": 100, "done": 40, "state": "moving", "speed": 10, "eta": 6, **over}


def test_both_ends_merge_and_a_finish_ends_the_leg():
    lv = SendLive()
    lv.report("phone", [snap()], now=0.0)
    lv.report("pc", [snap(done=55)], now=0.1)  # the receiver is ahead: the higher count wins
    assert [i["done"] for i in lv.items(0.2)] == [55]
    lv.report("phone", [snap(done=50)], now=0.3)  # never goes backwards
    assert lv.items(0.3)[0]["done"] == 55
    lv.report("pc", [snap(done=100, state="done")], now=0.4)
    lv.report("phone", [snap(done=90)], now=0.5)  # a late report from the other end doesn't reopen it
    assert lv.items(5.0)[0]["state"] == "done" and lv.items(11.0) == []


def test_held_share_legs_are_separate():
    lv = SendLive()
    base = snap(offer_id="h" * 8, to="calypso", total=9, done=9, state="done", eta=None)
    lv.report("phone", [base], now=0.0)
    lv.report("calypso", [{**base, "leg": "hold", "from": "calypso", "to": "pc", "done": 1, "state": "moving"}], 0.1)
    assert {(i["leg"], i["state"]) for i in lv.items(0.2)} == {("send", "done"), ("hold", "moving")}


def test_cancel_reaches_the_sender_on_its_next_report():
    lv = SendLive()
    s = snap(offer_id="c" * 8, **{"from": "pc", "to": "phone"})
    lv.report("pc", [s], now=0.0)
    assert lv.cancel("c" * 8) and not lv.cancel("x" * 8)
    assert lv.report("phone", [], now=0.1) == []  # not the sender
    assert lv.report("pc", [s], now=0.2) == ["c" * 8]
    assert lv.report("pc", [s], now=0.3) == []  # asked once


def test_the_routes(tmp_path):
    with TestClient(create_app(make(tmp_path))) as c:
        h = {"Authorization": "Bearer t0ken"}
        assert c.post("/send/live", json={"device": "pc", "items": []}).status_code == 401
        s = snap(**{"from": "pc", "to": "phone"})
        assert c.post("/send/live", json={"device": "pc", "items": [s]}, headers=h).json() == {"cancel": []}
        rows = c.get("/send/live", headers=h).json()
        assert rows[0]["device"] == "pc" and rows[0]["done"] == 40
        assert c.post("/send/live/" + "o" * 8 + "/cancel", headers=h).json() == {"ok": True}
        assert c.post("/send/live", json={"device": "pc", "items": [s]}, headers=h).json() == {"cancel": ["o" * 8]}
        c.post("/send/live", json={"device": "pc", "items": [{**s, "done": 100, "state": "done"}]}, headers=h)
        c.post("/send/live", json={"device": "pc", "items": [{**s, "done": 100, "state": "done"}]}, headers=h)
        evs = c.get("/events?kinds=send.live&limit=10", headers=h).json()["events"]
        assert [e["data"]["state"] for e in evs] == ["moving", "done"]  # one when it starts, one when it ends
        assert all(e.get("from") is None for e in evs)  # no lines on the main map


def test_a_leg_nobody_reports_on_goes_away():
    """An agent that lost its last report (or went to sleep mid-transfer) must not leave a leg moving forever."""
    lv = SendLive()
    lv.report("pc", [snap(offer_id="s" * 8)], now=0.0)
    assert len(lv.items(20.0)) == 1
    assert lv.items(31.0) == []
