"""Helpers for the Argus Send tests: in-process agents on localhost (own ports, data dirs and roots)."""

from __future__ import annotations

import asyncio
import socket
import time
from pathlib import Path

import httpx

from argus.config import SendConfig
from argus.send import agent as agent_mod
from argus.send.agent import SendAgent

agent_mod.POLL_SECONDS = 0.05


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_agent(tmp: Path, device_id: str, name: str, *, transport=None, **over) -> SendAgent:
    port = over.pop("port", None) or free_port()
    kw = dict(enabled=True, device_id=device_id, name=name, host="127.0.0.1", port=port, mdns=False,
              data_dir=str(tmp / f"{device_id}-data"), root=str(tmp / f"{device_id}-root"),
              lan=[f"127.0.0.1:{port}"], chunk_bytes=16 * 1024, retry_seconds=0.1, lan_probe_seconds=0.1,
              probe_timeout_seconds=1, register_seconds=0.2)
    kw.update(over)
    return SendAgent(SendConfig(**kw), transport=transport)


def pair(a: SendAgent, b: SendAgent, *, both: bool = True) -> None:
    """Teach the agents each other (what pairing through Argus, or devices.json, does)."""
    a.devices.put(b.me())
    if both:
        b.devices.put(a.me())


async def wait_for(cond, timeout: float = 30.0, what: str = "condition"):
    end = time.time() + timeout
    while time.time() < end:
        got = cond()
        if got:
            return got
        await asyncio.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


async def wait_state(agent: SendAgent, oid: str, state: str, timeout: float = 30.0) -> dict:
    return await wait_for(lambda: (i := agent.store.get(oid))["state"] == state and i, timeout,
                          f"{oid[:6]} to be {state} (is {agent.store.get(oid)['state']}: "
                          f"{agent.store.get(oid)['reason']})")


def make_file(path: Path, size: int, seed: int = 1) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = bytes((i * 31 + seed) % 251 for i in range(size))
    path.write_bytes(data)
    return data


class Spy(httpx.AsyncBaseTransport):
    """The real transport with hooks: kill PUTs, damage one body, slow things down, count what went where."""

    def __init__(self):
        self.inner = httpx.AsyncHTTPTransport()
        self.puts: list[tuple[int, str]] = []  # (port, query) of each chunk PUT that was forwarded
        self.kill_after: int | None = None  # after this many forwarded PUTs every PUT fails until healed
        self.corrupt_once = False
        self.delay = 0.0
        self.on_put = None
        self.sniffed: list[tuple[str, str, dict, bytes]] = []  # what a third party on the LAN sees

    def heal(self) -> None:
        self.kill_after = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.sniffed.append((request.method, str(request.url), dict(request.headers), await request.aread()))
        if request.method == "PUT":
            if self.kill_after is not None and len(self.puts) >= self.kill_after:
                raise httpx.ConnectError("connection killed", request=request)
            if self.corrupt_once:
                self.corrupt_once = False
                body = bytearray(await request.aread())
                body[0] ^= 0xFF
                request = httpx.Request(request.method, request.url, headers=request.headers, content=bytes(body))
            self.puts.append((request.url.port, request.url.query.decode()))
            if self.on_put:
                self.on_put(len(self.puts))
            if self.delay:
                await asyncio.sleep(self.delay)
        return await self.inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self.inner.aclose()


def tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}
