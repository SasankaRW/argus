"""Fake Ollama and fake claude for tests: behave well, send garbage, hang, error, or be missing."""

from __future__ import annotations

import json
import socket
import sys
import textwrap
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class FakeOllama:
    """A tiny HTTP server speaking Ollama's /api/chat and /api/tags.

    `replies` maps model name -> list of behaviours used in order (the last one repeats):
      a dict/str  -> returned as the assistant's content (dicts as JSON)
      "HANG"      -> sleep past any sane timeout
      "ERROR"     -> HTTP 500
    Unknown models get HTTP 404 like a model that was never pulled.
    """

    def __init__(self, replies: dict[str, list]):
        self.replies = {k: list(v) for k, v in replies.items()}
        self.requests: list[dict] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def _send(self, code: int, body: dict):
                raw = json.dumps(body).encode()
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_GET(self):
                if self.path == "/api/tags":
                    self._send(200, {"models": [{"name": m} for m in outer.replies]})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(body)
                model = body.get("model")
                if model not in outer.replies:
                    self._send(404, {"error": f"model '{model}' not found"})
                    return
                if self.path == "/api/embed":  # words hashed into 64 numbers: same words, close vectors
                    import hashlib

                    def vec(t: str) -> list[float]:
                        v = [0.0] * 64
                        for w in t.lower().split():
                            v[int(hashlib.md5(w.strip(".,!?").encode()).hexdigest(), 16) % 64] += 1.0
                        return v

                    self._send(200, {"model": model, "embeddings": [vec(t) for t in body["input"]]})
                    return
                queue = outer.replies[model]
                what = queue.pop(0) if len(queue) > 1 else queue[0]
                if what == "HANG":
                    time.sleep(5)
                    return
                if what == "ERROR":
                    self._send(500, {"error": "boom"})
                    return
                content = what if isinstance(what, str) else json.dumps(what)
                if body.get("stream"):  # like Ollama: one JSON line per piece, then a last line with the totals
                    lines = [{"model": model, "message": {"role": "assistant", "content": content[i:i + 8]},
                              "done": False} for i in range(0, len(content), 8)]
                    lines.append({"model": model, "message": {"role": "assistant", "content": ""}, "done": True,
                                  "eval_count": 7})
                    raw = "".join(json.dumps(x) + "\n" for x in lines).encode()
                    try:
                        self.send_response(200)
                        self.send_header("Content-Type", "application/x-ndjson")
                        self.send_header("Content-Length", str(len(raw)))
                        self.end_headers()
                        self.wfile.write(raw)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    return
                self._send(200, {"model": model, "message": {"role": "assistant", "content": content},
                                 "done": True, "eval_count": 7})

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> FakeOllama:
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()


DEFAULT_RESULT = '{"category": "Documents", "reason": "claude"}'


def fake_claude(tmp: Path, mode: str = "ok", result: str = DEFAULT_RESULT) -> list[str]:
    """Write a script that behaves like `claude -p --output-format json` and return the command for it.
    It records its argv and stdin next to itself."""
    script = tmp / f"fake_claude_{mode}.py"
    script.write_text(textwrap.dedent(f"""
        import json, sys, time, pathlib
        here = pathlib.Path(__file__).parent
        (here / "claude_argv.json").write_text(json.dumps(sys.argv[1:]))
        sys.stdin.reconfigure(encoding="utf-8")
        (here / "claude_stdin.txt").write_text(sys.stdin.read(), encoding="utf-8")
        mode = {mode!r}
        if mode == "hang":
            time.sleep(10)
        elif mode == "error":
            print(json.dumps({{"type": "result", "subtype": "error_max_turns", "is_error": True, "result": "nope"}}))
        elif mode == "crash":
            sys.stderr.write("Invalid API key. Please run /login")
            sys.exit(1)
        else:
            print(json.dumps({{"type": "result", "subtype": "success", "is_error": False,
                               "result": {result!r}, "total_cost_usd": 0.01, "num_turns": 1}}))
    """), encoding="utf-8")
    return [sys.executable, str(script)]


class FakePhoneApp:
    """What the Argus phone app does: collects its notifications from `GET /phone/inbox`, acknowledges them and
    taps their buttons. Used with a running Argus (`base` is its URL)."""

    def __init__(self, base: str, token: str = "tok"):
        self.base = base.rstrip("/")
        self.token = token
        self.messages: list[dict] = []

    def _call(self, method: str, path: str, body: dict | None = None, auth: bool = True) -> tuple[int, dict]:
        import urllib.error
        import urllib.request

        data = None if method == "GET" else (json.dumps(body).encode() if body is not None else b"")
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if auth:
            req.add_header("Authorization", f"Bearer {self.token}")
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def pull(self, wait: float = 0) -> list[dict]:
        """One round of the app's loop: ask, remember, acknowledge. Returns the new messages."""
        code, out = self._call("GET", f"/phone/inbox?device=test-phone&wait={wait}")
        assert code == 200, out
        got = out["messages"]
        if got:
            self._call("POST", "/phone/inbox/ack", {"ids": [m["id"] for m in got]})
            self.messages.extend(got)
        return got

    def wait_messages(self, n: int, timeout: float = 8.0) -> list[dict]:
        end = time.monotonic() + timeout
        while len(self.messages) < n and time.monotonic() < end:
            self.pull(wait=0.5)
        return self.messages

    def tap(self, action: dict) -> tuple[int, dict]:
        """A button: the url is a path on Argus (the app puts its own address in front); no Argus token is sent,
        the one-time token is in the url."""
        url = action["url"]
        path = url if url.startswith("/") else "/" + url.split("/", 3)[3]
        return self._call(action.get("method", "POST"), path, auth=False)
