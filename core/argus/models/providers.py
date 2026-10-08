"""Model providers: one call to one model, with a hard timeout. Standard library only.

- `OllamaProvider` talks to Ollama's /api/chat (non-streaming, temperature 0, structured JSON output when a
  schema is given, `keep_alive` so the model stays loaded between calls).
- `ClaudeProvider` runs the `claude` CLI in print mode with every tool removed: text in, text out.

Both raise a `ModelError` subclass on failure. `ModelTimeout` and `ModelUnavailable` mean the model could
not answer (these trip circuit breakers); a reply that turns out to be wrong is the router's business.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any


class ModelError(Exception):
    """A model call failed."""


class ModelTimeout(ModelError):
    """No answer within the timeout."""


class ModelUnavailable(ModelError):
    """The model or its server is missing, down, logged out, or refused the call."""


@dataclass
class Reply:
    text: str
    latency_ms: float
    meta: dict[str, Any] = field(default_factory=dict)


def _streamed(resp, on_text) -> dict[str, Any]:
    """Ollama's streamed answer (one JSON object per line) read as it comes; the last line carries the totals.
    Returns what a non-streamed answer would have been."""
    text, last = "", {}
    for line in resp:
        line = line.strip()
        if not line:
            continue
        last = json.loads(line)
        if "error" in last:
            return last
        piece = (last.get("message") or {}).get("content", "")
        if piece:
            text += piece
            try:
                on_text(text)
            except Exception:  # noqa: BLE001 - a listener's problem never breaks the answer
                pass
        if last.get("done"):
            break
    return {**last, "message": {"role": "assistant", "content": text}}


def context_for(system: str, messages: list[dict[str, Any]], schema: dict | None = None, base: int = 8192) -> int:
    """The context size to ask for: always `base` (one size, so Ollama never reloads the model to change it), more
    only for a prompt too long for it (Ollama would quietly cut it, and the question with it). About 3 characters
    a token, plus room for the answer."""
    size = len(system) + sum(len(str(m.get("content") or "")) for m in messages)
    size += len(json.dumps(schema)) if schema else 0
    est = size // 3 + 1024
    n = base
    while n < est and n < 32768:
        n *= 2
    return n


class OllamaProvider:
    kind = "ollama"

    def __init__(self, url: str, model: str, *, timeout: float = 120, keep_alive: str = "10m", num_ctx: int = 8192):
        self.num_ctx = num_ctx
        self.url = url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.keep_alive = keep_alive

    def chat(self, system: str, messages: list[dict[str, Any]], schema: dict | None = None, *,
             on_text: Any = None, temperature: float = 0.0) -> Reply:
        """`on_text(text_so_far)`: called as the answer arrives (Ollama streams it), so a reply can be spoken
        before it is finished. `temperature`: 0 for picking tools and filling JSON (the same input, the same
        answer); higher for talk, so Ari doesn't say the same thing the same way every time."""
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *messages],
            "stream": on_text is not None,
            "keep_alive": self.keep_alive,
            "options": {"temperature": max(0.0, min(1.5, float(temperature)))},
        }
        body["options"]["num_ctx"] = context_for(system, messages, schema, self.num_ctx)
        if re.search(r"qwen3(?!-coder)|deepseek-r1", self.model, re.I):
            body["think"] = False  # these think out loud first by default: many seconds before a short answer
        if schema is not None:
            body["format"] = schema  # Ollama structured outputs: the reply is constrained to this JSON schema
        req = urllib.request.Request(f"{self.url}/api/chat", data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = _streamed(resp, on_text) if on_text is not None else json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            if e.code == 404:
                raise ModelUnavailable(f"{self.model} is not pulled on {self.url} (ollama pull {self.model})") from None
            raise ModelUnavailable(f"Ollama HTTP {e.code}: {detail}") from None
        except (TimeoutError, OSError) as e:  # URLError is an OSError; socket timeouts too
            if _is_timeout(e):
                raise ModelTimeout(f"{self.model} gave no answer within {self.timeout:g} s") from None
            raise ModelUnavailable(f"Ollama not reachable at {self.url}: {e}") from None
        except ValueError as e:
            raise ModelUnavailable(f"Ollama sent something that is not JSON: {e}") from None
        latency = (time.perf_counter() - t0) * 1000
        if "error" in data:
            raise ModelUnavailable(f"Ollama: {data['error']}")
        text = (data.get("message") or {}).get("content", "")
        meta = {k: data[k] for k in ("eval_count", "prompt_eval_count", "total_duration", "load_duration",
                                       "prompt_eval_duration", "eval_duration")
                if k in data}
        return Reply(text, latency, meta)

    def embed(self, texts: list[str], model: str) -> list[list[float]]:
        """Vectors for `texts` with an embedding model (e.g. nomic-embed-text), for search by meaning."""
        body = {"model": model, "input": texts, "keep_alive": self.keep_alive, "truncate": True}
        req = urllib.request.Request(f"{self.url}/api/embed", data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise ModelUnavailable(f"{model} is not pulled on {self.url} (ollama pull {model})") from None
            raise ModelUnavailable(f"Ollama HTTP {e.code}: {e.read().decode(errors='replace')[:200]}") from None
        except (TimeoutError, OSError, ValueError) as e:
            raise ModelUnavailable(f"Ollama not reachable at {self.url}: {e}") from None
        vecs = data.get("embeddings")
        if not isinstance(vecs, list) or len(vecs) != len(texts):
            raise ModelUnavailable(f"Ollama: no embeddings ({str(data)[:200]})")
        return vecs

    def list_models(self) -> list[str]:
        try:
            with urllib.request.urlopen(f"{self.url}/api/tags", timeout=min(self.timeout, 10)) as resp:
                return [m["name"] for m in json.loads(resp.read()).get("models", [])]
        except (OSError, ValueError) as e:
            raise ModelUnavailable(f"Ollama not reachable at {self.url}: {e}") from None


class ClaudeProvider:
    kind = "claude"

    def __init__(self, command: list[str], args: list[str], *, timeout: float = 300, model: str | None = None):
        self.command = list(command)
        self.args = list(args)
        self.timeout = timeout
        self.model = model

    def available(self) -> bool:
        return bool(self.command) and (shutil.which(self.command[0]) is not None or os.path.exists(self.command[0]))

    def argv(self, stream: bool = False, web: bool = False) -> list[str]:
        # Resolve the program (on Windows `claude` is claude.cmd, which needs its full path to start).
        # Nothing from the playbook or the input goes on the command line: on Windows cmd.exe would cut it at the
        # first newline and could read & | % in it as commands. All text goes through stdin.
        program = shutil.which(self.command[0]) or self.command[0]
        args = list(self.args)
        if web:  # general questions that need the internet: web search and fetch only, nothing else
            if "--disallowedTools" in args:
                i = args.index("--disallowedTools")
                del args[i:i + 2]
            if "--max-turns" in args:
                args[args.index("--max-turns") + 1] = "8"
            args += ["--allowedTools", "WebSearch,WebFetch"]
        if stream:  # pictures: the message goes in as JSON (stream-json), the answer comes back as JSON lines
            if "--output-format" in args:
                i = args.index("--output-format")
                del args[i:i + 2]
            args += ["--input-format", "stream-json", "--output-format", "stream-json", "--verbose"]
        argv = [program, *self.command[1:], *args]
        if self.model:
            argv += ["--model", self.model]
        return argv

    @staticmethod
    def _media_type(b64: str) -> str:
        head = base64.b64decode(b64[:24] + "=" * (-len(b64[:24]) % 4))
        if head.startswith(b"\x89PNG"):
            return "image/png"
        if head.startswith(b"GIF8"):
            return "image/gif"
        if head[8:12] == b"WEBP":
            return "image/webp"
        return "image/jpeg"

    def auth_status(self) -> tuple[bool | None, str]:
        """(logged in?, detail) from `claude auth status` (no call, no cost). None: can't tell."""
        if not self.available():
            return None, "claude CLI not found"
        program = shutil.which(self.command[0]) or self.command[0]
        try:
            p = subprocess.run([program, *self.command[1:], "auth", "status"], capture_output=True, text=True,
                               timeout=30, stdin=subprocess.DEVNULL)
            info = json.loads(p.stdout or "{}")
        except (OSError, subprocess.TimeoutExpired, ValueError) as e:
            return None, str(e)[:200]
        if "loggedIn" not in info:
            return None, (p.stderr or p.stdout)[:200]
        return bool(info["loggedIn"]), str(info.get("authMethod") or "")

    def chat(self, system: str, messages: list[dict[str, Any]], schema: dict | None = None, *,
             web: bool = False) -> Reply:
        """`web`: may search and read the web (for current things). Claude sees the pictures too (`images` on a
        message, base64), like the vision tier."""
        images = [i for m in messages for i in (m.get("images") or [])]
        prompt = "\n\n".join(m["content"] for m in messages if m["role"] == "user")
        if system:
            prompt = f"{system}\n\n---\n\n{prompt}"
        if schema is not None:
            prompt += ("\n\nAnswer with only a JSON object matching this JSON schema, no other text:\n"
                       + json.dumps(schema))
        if not self.available():
            raise ModelUnavailable(f"{self.command[0]!r} not found (is Claude Code installed?)")
        t0 = time.perf_counter()
        stdin = prompt
        if images:
            content = [{"type": "image", "source": {"type": "base64", "media_type": self._media_type(b), "data": b}}
                       for b in images] + [{"type": "text", "text": prompt}]
            stdin = json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n"
        try:
            code, stdout, stderr = run_with_timeout(self.argv(stream=bool(images), web=web), stdin, self.timeout)
        except subprocess.TimeoutExpired:
            raise ModelTimeout(f"claude gave no answer within {self.timeout:g} s") from None
        except OSError as e:
            raise ModelUnavailable(f"could not run claude: {e}") from None
        latency = (time.perf_counter() - t0) * 1000
        if images:  # JSON lines: the last "result" line is the answer
            stdout = next((ln for ln in reversed(stdout.splitlines()) if '"type":"result"' in ln.replace(" ", "")),
                          stdout)
        try:
            out = json.loads(stdout)
        except ValueError:
            raise ModelUnavailable(f"claude exited {code}: {(stderr or stdout).strip()[:300]}") from None
        if out.get("is_error") or out.get("subtype", "success") != "success":
            raise ModelUnavailable(f"claude reported an error: {str(out.get('result') or out)[:300]}")
        meta = {k: out[k] for k in ("total_cost_usd", "duration_ms", "num_turns", "session_id") if k in out}
        return Reply(str(out.get("result", "")), latency, meta)


def run_with_timeout(argv: list[str], stdin: str, timeout: float) -> tuple[int, str, str]:
    """Run a program with a hard timeout that also ends its children.

    On Windows `claude` is claude.cmd -> cmd.exe -> node: killing only cmd.exe would leave node holding the pipes
    and the worker waiting forever, so the whole tree is ended (taskkill /T). On Linux it gets its own process
    group, which is killed as a whole.
    """
    kw: dict[str, Any] = {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
                          "text": True, "encoding": "utf-8", "errors": "replace"}
    if os.name == "nt":
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    p = subprocess.Popen(argv, **kw)
    try:
        out, err = p.communicate(stdin, timeout=timeout)
        return p.returncode, out, err
    except subprocess.TimeoutExpired:
        _kill_tree(p)
        with contextlib.suppress(Exception):
            p.communicate(timeout=5)
        raise


def _kill_tree(p: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True, timeout=10, check=False)
    else:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(p.pid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        p.kill()


def _is_timeout(e: BaseException) -> bool:
    if isinstance(e, TimeoutError):
        return True
    reason = getattr(e, "reason", None)
    return isinstance(reason, TimeoutError) or "timed out" in str(e).lower()
