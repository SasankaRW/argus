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


class FakeNtfy:
    """A tiny ntfy server.

    POST /            JSON publish (what Argus sends); recorded in `messages`. `fail_next` makes the next N fail.
    POST /<topic>     plain-text publish (what the phone's http buttons do); kept per topic.
    GET  /<topic>/json?poll=1&since=<id|unix time>   the cached messages after `since`, one JSON per line.
    """

    def __init__(self, fail_next: int = 0):
        self.messages: list[dict] = []
        self.topics: dict[str, list[dict]] = {}
        self.polls: list[str] = []
        self.fail_next = fail_next
        self.headers: list[dict] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, code: int, body: bytes, ctype: str = "application/json"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if outer.fail_next > 0:
                    outer.fail_next -= 1
                    self._reply(500, b'{"error":"boom"}')
                    return
                topic = self.path.strip("/")
                if topic:  # plain publish to a topic
                    msg = outer.publish(topic, raw.decode())
                    self._reply(200, json.dumps(msg).encode())
                    return
                body = json.loads(raw)
                outer.messages.append(body)
                outer.headers.append(dict(self.headers))
                self._reply(200, json.dumps({"id": str(len(outer.messages)), "event": "message"}).encode())

            def do_GET(self):
                from urllib.parse import parse_qs, urlparse

                u = urlparse(self.path)
                topic = u.path.strip("/").removesuffix("/json")
                since = parse_qs(u.query).get("since", ["all"])[0]
                outer.polls.append(since)
                msgs = outer.topics.get(topic, [])
                ids = [m["id"] for m in msgs]
                if since in ids:
                    msgs = msgs[ids.index(since) + 1:]
                elif since.isdigit():
                    msgs = [m for m in msgs if m["time"] >= int(since)]
                self._reply(200, "".join(json.dumps(m) + "\n" for m in msgs).encode(), "application/x-ndjson")

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def publish(self, topic: str, text: str) -> dict:
        lst = self.topics.setdefault(topic, [])
        msg = {"id": f"m{len(lst) + 1:04d}", "time": int(time.time()), "event": "message", "topic": topic,
               "message": text}
        lst.append(msg)
        return msg

    def tap(self, action: dict) -> int:
        """What the ntfy app does for an http button."""
        import urllib.request

        req = urllib.request.Request(action["url"], data=(action.get("body") or "").encode(),
                                     method=action.get("method", "POST"))
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status

    def __enter__(self) -> FakeNtfy:
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()
