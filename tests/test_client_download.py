"""A download from argusd that is cut off partway carries on from where it stopped (backups over Tailscale)."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from argus.worker.client import ArgusClient, Unreachable

DATA = bytes(range(256)) * 4000  # ~1 MB


def serve(cut_first: int, ranges: bool = True, cut_always: bool = False):
    seen = {"requests": [], "n": 0}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):  # noqa: N802
            seen["n"] += 1
            rng = self.headers.get("Range")
            seen["requests"].append(rng)
            start = int(rng.split("=")[1].rstrip("-")) if rng and ranges else 0
            body = DATA[start:]
            self.send_response(206 if start else 200)
            if start:
                self.send_header("Content-Range", f"bytes {start}-{len(DATA) - 1}/{len(DATA)}")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            cut = cut_always or seen["n"] == 1
            self.wfile.write(body[:cut_first] if cut else body)
            if cut:
                self.connection.shutdown(2)  # the connection drops partway

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, seen


def test_a_cut_off_download_goes_on_from_where_it_stopped(monkeypatch):
    monkeypatch.setattr("argus.worker.client.time.sleep", lambda s: None)
    srv, seen = serve(cut_first=300_000)
    try:
        got = ArgusClient(f"http://127.0.0.1:{srv.server_address[1]}").get_bytes("/backups/files/x")
    finally:
        srv.shutdown()
    assert got == DATA
    assert seen["requests"][0] is None and seen["requests"][1].startswith("bytes=")


def test_a_server_without_ranges_sends_it_all_again(monkeypatch):
    monkeypatch.setattr("argus.worker.client.time.sleep", lambda s: None)
    srv, seen = serve(cut_first=300_000, ranges=False)
    try:
        got = ArgusClient(f"http://127.0.0.1:{srv.server_address[1]}").get_bytes("/x")
    finally:
        srv.shutdown()
    assert got == DATA


def test_it_gives_up_after_a_few_tries(monkeypatch):
    monkeypatch.setattr("argus.worker.client.time.sleep", lambda s: None)
    srv, seen = serve(cut_first=10_000, ranges=False, cut_always=True)
    try:
        with pytest.raises(Unreachable):
            ArgusClient(f"http://127.0.0.1:{srv.server_address[1]}").get_bytes("/x", tries=3)
    finally:
        srv.shutdown()
    assert seen["n"] == 3
