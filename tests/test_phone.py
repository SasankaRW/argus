"""Find my phone: where Tailscale sees it, and a loud ring (three urgent notifications)."""

from __future__ import annotations

import json

from argus.presence import describe_phone, phone_info
from conftest import run
from test_ask import make
from test_worker import Server, client

STATUS = {"Peer": {
    "a": {"HostName": "pixel-8a", "DNSName": "pixel-8a.tail123.ts.net.", "Online": True,
          "CurAddr": "192.168.1.23:41641", "LastSeen": "2026-09-30T10:00:00Z"},
    "b": {"HostName": "laptop", "DNSName": "laptop.tail123.ts.net.", "Online": False, "CurAddr": "",
          "LastSeen": "2026-09-29T20:15:00Z"}}}


def test_home_away_offline():
    assert phone_info(STATUS, "pixel-8a")["where"] == "home"
    away = json.loads(json.dumps(STATUS))
    away["Peer"]["a"]["CurAddr"] = ""
    away["Peer"]["a"]["Relay"] = "sin"
    i = phone_info(away, "pixel-8a")
    assert i["where"] == "away" and i["via"] == "relay sin"
    off = phone_info(STATUS, "laptop")
    assert off["online"] is False and "last online 2026-09-29 20:15" in describe_phone(off)
    assert phone_info(STATUS, "nokia") is None


def test_ari_finds_and_rings_the_phone(tmp_path, monkeypatch):
    a = make(tmp_path, monkeypatch)
    y = (tmp_path / "argus.yaml").read_text() + "approvals:\n  phone: pixel-8a\nntfy:\n  url: http://127.0.0.1:9\n"
    (tmp_path / "argus.yaml").write_text(y)
    monkeypatch.setenv("NTFY_TOPIC", "argus-test-topic")
    from argus.config import load_config
    from argus.context import Argus

    a = Argus(load_config(tmp_path / "argus.yaml"))
    a.phone._status = lambda: STATUS
    a.outbox.poll_seconds = 3600  # keep the messages in the outbox to look at them
    with Server(a.open()) as srv:
        cl = client(srv.url)
        r = cl.post("/ari", {"text": "where's my phone?"})
        assert "at home" in r["reply"] and r["pending"] == {"kind": "action", "action": "phone:ring"}
        done = cl.post(f"/ari/{r['conv']}/answer", {"yes": True})
        assert done["reply"] == "Ringing it now." and done["phone"]["times"] == 3
        assert cl.get("/phone")["info"]["where"] == "home"
        rows = run(a.store.read(lambda c: [(json.loads(r[0]), r[1]) for r in
                                           c.execute("SELECT payload, next_try_at FROM outbox ORDER BY next_try_at")]))
    rings = [(p, t) for p, t in rows if p.get("title") == "Here I am!"]
    assert len(rings) == 3 and rings[0][0]["priority"] == 5
    assert rings[2][1] - rings[1][1] >= 19  # 20 s apart (the first may be retrying)
