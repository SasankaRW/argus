"""Model providers: one call to one model, with a hard timeout. Standard library only.

- `OllamaProvider` talks to Ollama's /api/chat (non-streaming, temperature 0, structured JSON output when a
  schema is given, `keep_alive` so the model stays loaded between calls).
- `ClaudeProvider` runs the `claude` CLI in print mode with every tool removed: text in, text out.

Both raise a `ModelError` subclass on failure. `ModelTimeout` and `ModelUnavailable` mean the model could
not answer (these trip circuit breakers); a reply that turns out to be wrong is the router's business.
"""

from __future__ import annotations

import json
import os
import shutil
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


class OllamaProvider:
    kind = "ollama"

    def __init__(self, url: str, model: str, *, timeout: float = 120, keep_alive: str = "10m"):
        self.url = url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.keep_alive = keep_alive

    def chat(self, system: str, messages: list[dict[str, str]], schema: dict | None = None) -> Reply:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *messages],
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": {"temperature": 0},
        }
        if schema is not None:
            body["format"] = schema  # Ollama structured outputs: the reply is constrained to this JSON schema
        req = urllib.request.Request(f"{self.url}/api/chat", data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read() or b"{}")
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
        meta = {k: data[k] for k in ("eval_count", "prompt_eval_count", "total_duration", "load_duration")
                if k in data}
        return Reply(text, latency, meta)

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

    def argv(self, system: str) -> list[str]:
        # Resolve the program (on Windows `claude` is claude.cmd, which needs its full path to start).
        program = shutil.which(self.command[0]) or self.command[0]
        argv = [program, *self.command[1:], *self.args, "--append-system-prompt", system]
        if self.model:
            argv += ["--model", self.model]
        return argv

    def chat(self, system: str, messages: list[dict[str, str]], schema: dict | None = None) -> Reply:
        prompt = "\n\n".join(m["content"] for m in messages if m["role"] == "user")
        if schema is not None:
            prompt += ("\n\nAnswer with only a JSON object matching this JSON schema, no other text:\n"
                       + json.dumps(schema))
        if not self.available():
            raise ModelUnavailable(f"{self.command[0]!r} not found (is Claude Code installed?)")
        t0 = time.perf_counter()
        try:
            r = subprocess.run(self.argv(system), input=prompt, capture_output=True, text=True,
                               timeout=self.timeout, encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            raise ModelTimeout(f"claude gave no answer within {self.timeout:g} s") from None
        except OSError as e:
            raise ModelUnavailable(f"could not run claude: {e}") from None
        latency = (time.perf_counter() - t0) * 1000
        try:
            out = json.loads(r.stdout)
        except ValueError:
            raise ModelUnavailable(f"claude exited {r.returncode}: {(r.stderr or r.stdout).strip()[:300]}") from None
        if out.get("is_error") or out.get("subtype", "success") != "success":
            raise ModelUnavailable(f"claude reported an error: {str(out.get('result') or out)[:300]}")
        meta = {k: out[k] for k in ("total_cost_usd", "duration_ms", "num_turns", "session_id") if k in out}
        return Reply(str(out.get("result", "")), latency, meta)


def _is_timeout(e: BaseException) -> bool:
    if isinstance(e, TimeoutError):
        return True
    reason = getattr(e, "reason", None)
    return isinstance(reason, TimeoutError) or "timed out" in str(e).lower()
