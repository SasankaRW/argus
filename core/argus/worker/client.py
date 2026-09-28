"""A tiny HTTP client for the Argus worker protocol. Standard library only, so a worker has few moving parts."""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any

log = logging.getLogger("argus.worker.client")

# Waits between attempts when Argus is unreachable (e.g. restarting). About 30 s in total.
RETRY_DELAYS = (0.5, 1, 2, 4, 8, 15)


class ApiError(Exception):
    def __init__(self, status: int, body: Any):
        self.status = status
        self.body = body
        detail = body.get("detail") if isinstance(body, dict) else body
        super().__init__(f"HTTP {status}: {detail}")

    @property
    def code(self) -> str | None:
        return self.body.get("error") if isinstance(self.body, dict) else None


class LeaseLostError(ApiError):
    """Argus says this worker no longer owns the job. Stop working on it; someone else will."""


class Unreachable(Exception):
    """Argus could not be reached after all retries."""


class ArgusClient:
    def __init__(self, url: str, token: str | None = None, timeout: float = 10.0,
                 retry_delays: tuple[float, ...] = RETRY_DELAYS):
        self.url = url.rstrip("/")
        self.token = token or None
        self.timeout = timeout
        self.retry_delays = retry_delays

    def _once(self, method: str, path: str, body: Any, timeout: float) -> tuple[int, Any]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                return resp.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                payload = json.loads(raw) if raw else None
            except ValueError:
                payload = raw.decode(errors="replace")
            if e.code == 409 and isinstance(payload, dict) and payload.get("error") in ("lease_lost",
                                                                                         "invalid_transition"):
                raise LeaseLostError(e.code, payload) from None
            raise ApiError(e.code, payload) from None

    def call(self, method: str, path: str, body: Any = None, *, timeout: float | None = None,
             retry: bool = True) -> tuple[int, Any]:
        """Send one request. Connection problems and 5xx are retried; 4xx answers are raised at once."""
        delays = self.retry_delays if retry else ()
        t = timeout or self.timeout
        for attempt in range(len(delays) + 1):
            try:
                return self._once(method, path, body, t)
            except ApiError as e:
                if e.status < 500 or attempt >= len(delays):
                    raise
                log.warning("argus error, retrying", extra={"path": path, "status": e.status})
            except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
                if attempt >= len(delays):
                    raise Unreachable(f"{self.url}{path}: {e}") from e
                log.warning("argus unreachable, retrying", extra={"path": path, "error": str(e)})
            time.sleep(delays[attempt])
        raise AssertionError("unreachable")  # pragma: no cover

    def post(self, path: str, body: Any = None, **kw) -> Any:
        return self.call("POST", path, body if body is not None else {}, **kw)[1]

    def get(self, path: str, **kw) -> Any:
        return self.call("GET", path, **kw)[1]

    # -------------------------------------------------------------- protocol

    def register(self, worker_id: str, host: str, capabilities: list[str], version: str | None) -> dict:
        return self.post("/workers/register", {"id": worker_id, "host": host, "capabilities": capabilities,
                                               "version": version})

    def claim(self, worker_id: str, capabilities: list[str], plugins: list[str] | None, wait: float) -> dict | None:
        status, body = self.call("POST", f"/workers/{worker_id}/claim",
                                 {"capabilities": capabilities, "plugins": plugins, "wait": wait},
                                 timeout=wait + self.timeout)
        return None if status == 204 else body

    def start(self, job_id: str, worker: str) -> dict:
        return self.post(f"/jobs/{job_id}/start", {"worker": worker})

    def heartbeat(self, job_id: str, worker: str) -> dict:
        return self.post(f"/jobs/{job_id}/heartbeat", {"worker": worker}, retry=False)

    def step(self, job_id: str, worker: str, idx: int, name: str, state: str, *, output: Any = None,
             error: str | None = None, tier: str | None = None) -> None:
        self.post(f"/jobs/{job_id}/steps", {"worker": worker, "idx": idx, "name": name, "state": state,
                                            "output": output, "error": error, "tier": tier})

    def succeed(self, job_id: str, worker: str, result: Any) -> dict:
        return self.post(f"/jobs/{job_id}/succeed", {"worker": worker, "result": result})

    def fail(self, job_id: str, worker: str, error: str, retryable: bool) -> dict:
        return self.post(f"/jobs/{job_id}/fail", {"worker": worker, "error": error, "retryable": retryable})

    def wait(self, job_id: str, worker: str, reason: str) -> dict:
        return self.post(f"/jobs/{job_id}/wait", {"worker": worker, "reason": reason})

    # -------------------------------------------------------------- models

    def permit(self, tier: str, worker: str, job_id: str | None) -> dict:
        return self.post(f"/models/{tier}/permit", {"worker": worker, "job_id": job_id})

    def report(self, tier: str, worker: str, job_id: str | None, ok: bool, latency_ms: float | None,
               error: str | None) -> None:
        self.post(f"/models/{tier}/report", {"worker": worker, "job_id": job_id, "ok": ok,
                                             "latency_ms": latency_ms, "error": error})

    def trace(self, job_id: str, worker: str, kind: str, *, src: str | None = None, dst: str | None = None,
              step: str | None = None, data: dict | None = None) -> None:
        self.post(f"/jobs/{job_id}/events", {"worker": worker, "kind": kind, "src": src, "dst": dst,
                                             "step": step, "data": data})
