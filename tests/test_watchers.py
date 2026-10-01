"""Watchers: reading pages and prices, what counts as a change, and a real Argus checking a local page."""

from __future__ import annotations

import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import argus.worker.plugins as wplugins
from argus.config import load_config
from argus.context import Argus
from argus.worker import Worker
from argus.worker.plugins import Http, PermissionDenied
from test_worker import Server, client, wait_for

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("t_watchers", ROOT / "plugins" / "watchers" / "plugin.py")
wt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wt)

SHOP = """<html><head><title>Shop</title><script>var t = 1;</script>
<script type="application/ld+json">{"@type": "Product", "name": "RTX 5080",
 "offers": {"@type": "Offer", "price": "%s", "priceCurrency": "LKR"}}</script></head>
<body><div>Now %s</div><h1>RTX 5080</h1><p>Price Rs. %s</p><p>In stock</p>
<h2>Keyboard</h2><p>Rs. 12,500</p></body></html>"""


def shop(price: str, clock: str = "10:00") -> str:
    return SHOP % (price.replace(",", ""), clock, price)


def test_page_text_and_prices():
    t = wt.page_text(shop("289,000"))
    assert "var t" not in t and "RTX 5080" in t.splitlines()
    assert wt.structured_price(shop("289,000")) == (289000.0, "LKR")
    assert wt.text_price("Price Rs. 289,000 In stock") == (289000.0, "Rs.")
    assert wt.text_price("now only $1,299.99!") == (1299.99, "$")
    assert wt.text_price("from 45 000 LKR") == (45000.0, "LKR")
    assert wt.text_price("3 items in cart") is None
    assert wt.narrow(t, "keyboard").startswith("Keyboard") and wt.narrow(t, "mouse") is None
    assert wt.fmt(289000, "LKR") == "LKR 289,000" and wt.fmt(1299.5, "$") == "$1,299.5" and wt.fmt(5, "Rs.") == "Rs 5"
    meta = ('<meta property="product:price:amount" content="45.50">'
            '<meta property="product:price:currency" content="EUR">')
    assert wt.structured_price(meta) == (45.5, "EUR")


def test_a_shared_links_note():
    assert wt.note_price("tell me when it's under Rs 250,000") == (250000.0, "")
    assert wt.note_price("RTX 5080 below $999") == (999.0, "RTX 5080")
    assert wt.note_price("RTX 5080") == (None, "RTX 5080")  # a model number is not a price


def test_what_counts():
    w = {"name": "RTX", "kind": "price", "below": None, "part": ""}
    first = wt.read(shop("289,000"), w)
    w["last"] = first
    assert wt.verdict(w, wt.read(shop("289,000", "11:00"), w)) is None  # the clock isn't a price change
    assert wt.verdict(w, wt.read(shop("279,000"), w)) == "RTX dropped to LKR 279,000 (was LKR 289,000)."
    assert wt.verdict(w, wt.read(shop("299,000"), w)) is None
    w["below"] = 280000
    assert wt.verdict(w, wt.read(shop("279,000"), w)) == "RTX is LKR 279,000, under your LKR 280,000."
    w["last"] = wt.read(shop("279,000"), w)
    assert wt.verdict(w, wt.read(shop("275,000"), w)) is None  # told once when it went under
    c = {"name": "Keyboard", "kind": "change", "part": "keyboard"}
    c["last"] = wt.read(shop("289,000"), c)
    assert wt.verdict(c, wt.read(shop("289,000", "11:00"), c)) is None  # the clock is outside the part
    page = shop("289,000").replace("Rs. 12,500", "Rs. 12,500</p><p>Back in stock")
    assert wt.verdict(c, wt.read(page, c)) == "Keyboard changed: Back in stock"


def test_a_name_that_turns_local_on_connect_is_refused(monkeypatch):
    """DNS rebinding: the name checks out as public, but the connection lands on this machine."""
    page = Page()
    h = Http("watchers", ["*"], lambda *a: None, is_public=lambda host: True)
    with pytest.raises(PermissionDenied, match="not a public address"):
        h.request("GET", page.url)


def test_star_means_public_websites_only():
    h = Http("watchers", ["*"], lambda *a: None, is_public=lambda host: host == "shop.example.com")
    assert h._check("https://shop.example.com/x")
    for bad in ("http://192.168.1.1/", "http://localhost:8600/", "file:///etc/passwd", "http://sas-pc/"):
        with pytest.raises(PermissionDenied, match="public websites"):
            h._check(bad)
    assert not wplugins.public_host("127.0.0.1") and not wplugins.public_host("10.0.0.5")
    assert not wplugins.public_host("100.101.102.103")  # the tailnet
    with pytest.raises(PermissionDenied, match="permissions.network"):
        Http("homelab", ["localhost"], lambda *a: None)._check("https://example.com/")


class Page:
    def __init__(self):
        self.body = shop("289,000")
        me = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                data = me.body.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/rtx"


def test_watch_by_talking_then_the_check_tells_the_phone(tmp_path, monkeypatch):
    monkeypatch.setattr(wplugins, "public_host", lambda host: True)  # the test page is local
    monkeypatch.setattr(wplugins, "public_address", lambda ip: True)
    page = Page()
    (tmp_path / "argus.yaml").write_text(
        "logging:\n  file: null\njobs:\n  watchdog_interval_seconds: 0.1\n"
        f"plugins:\n  dirs: ['{(ROOT / 'plugins').as_posix()}']\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    argus = Argus(load_config(tmp_path / "argus.yaml"))
    with Server(argus.open()) as srv:
        cl = client(srv.url)
        w = Worker(cl, "laptop", capabilities=["cpu"], watch_folders=False)
        w.register()
        assert "watchers" in w.plugins, w.plugin_errors

        def call(workflow, **inp):
            job = cl.post("/jobs", {"plugin": "watchers", "workflow": workflow, "input": inp})
            w.run_once(wait=1)
            j = wait_for(lambda: (x := cl.get(f"/jobs/{job['id']}"))["state"] in ("succeeded", "dead") and x)
            return j

        added = call("add", url=page.url, kind="price", below=280000, name="RTX 5080")["result"]
        assert added["say"] == "Watching RTX 5080: it's LKR 289,000 now; I'll tell you when it's under LKR 280,000."
        bad = call("add", url=page.url, kind="price", part="mouse")
        assert bad["state"] == "dead" and "mouse" in bad["error"]
        page.body = shop("279,000")
        r = call("check")["result"]
        assert r == {"checked": 1, "told": ["RTX 5080 is LKR 279,000, under your LKR 280,000."], "problems": []}
        msgs = argus.store.read_sync(lambda c: [json.loads(x[0]) for x in c.execute("SELECT payload FROM outbox")])
        tell = [m for m in msgs if m.get("title") == "Watcher: RTX 5080"]
        assert len(tell) == 1 and tell[0]["priority"] >= 4 and tell[0].get("click") == page.url
        listed = call("list")["result"]["watches"]
        assert listed[0]["price"] == "LKR 279,000" and listed[0]["tell_under"] == "LKR 280,000"
        assert call("check")["result"]["told"] == []  # told once
        assert call("remove", name="rtx")["result"] == {"stopped": "RTX 5080"}
        shared = call("add", url=page.url, title="Keyboard deal", note="Keyboard under 13,000")["result"]
        assert shared["say"] == "Watching Keyboard deal: it's Rs 12,500 now; I'll tell you when it's under Rs 13,000."
        assert call("remove", name="keyboard deal")["result"] == {"stopped": "Keyboard deal"}
        assert call("list")["result"]["watches"] == []
