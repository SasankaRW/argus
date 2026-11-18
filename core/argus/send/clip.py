"""Clipboard and link sync (docs/superpowers/specs/2026-11-18-clipboard-sync-design.md): the logic, without Win32.

Wire: POST /send/clip with the usual session MAC headers; the body is {"n": b64 nonce, "c": b64 ciphertext}, AES-256-GCM
under HKDF-SHA256(session key, info "argus clip v1"). The plaintext is JSON {"id", "kind", "text", "at", "from", "for"}.
Clip text is never logged and never written to disk; the holder (Calypso) keeps a pending clip in RAM only.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urlsplit

import httpx
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .keys import b64, unb64
from .sender import Link, Refused

if TYPE_CHECKING:
    from .agent import SendAgent

log = logging.getLogger("argus.send")

INFO = b"argus clip v1"
MAX_BYTES = 256 * 1024  # larger text is not sent
DEBOUNCE = 0.3
SEEN_KEEP = 64
SKEW_MS = 5 * 60 * 1000  # a clip this much older than the newest one from that sender is dropped
PENDING_TTL = 600.0
PENDING_EVERY = 30.0
PROBE_BUDGET = 3.0
KINDS = ("copy", "link")
URL = re.compile(r"^https?://[^\s]+$", re.IGNORECASE)


class ClipError(Exception):
    """A clip could not be sent (nobody reachable) or was refused."""


class ClipDisabled(ClipError):
    """Clipboard sync is off here or on the other device (409)."""


# ---------------------------------------------------------------------- the wire format (pure)

def clip_key(session_key: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=INFO).derive(session_key)


def seal(session_key: bytes, clip: dict, nonce: bytes | None = None) -> bytes:
    """The request body for a clip: JSON {"n", "c"}."""
    nonce = nonce or os.urandom(12)
    plain = json.dumps(clip, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ct = AESGCM(clip_key(session_key)).encrypt(nonce, plain, None)
    return json.dumps({"n": b64(nonce), "c": b64(ct)}).encode("ascii")


def unseal(session_key: bytes, body: bytes) -> dict:
    """The clip in a request body; ValueError when it is not a well-formed, authentic clip."""
    try:
        d = json.loads(body)
        plain = AESGCM(clip_key(session_key)).decrypt(unb64(d["n"]), unb64(d["c"]), None)
        clip = json.loads(plain)
    except (ValueError, KeyError, TypeError, InvalidTag) as e:
        raise ValueError("not a clip") from e
    if not isinstance(clip, dict):
        raise ValueError("not a clip")
    return clip


def check_clip(c: dict) -> str | None:
    """Why a decoded clip is unusable, or None."""
    if not isinstance(c.get("id"), str) or not 1 <= len(c["id"]) <= 64:
        return "bad id"
    if c.get("kind") not in KINDS:
        return "bad kind"
    text = c.get("text")
    if not isinstance(text, str) or not text or len(text.encode("utf-8")) > MAX_BYTES:
        return "bad text"
    if not isinstance(c.get("at"), int) or isinstance(c["at"], bool):
        return "bad time"
    if not isinstance(c.get("from"), str) or not c["from"]:
        return "bad sender"
    if c.get("for") is not None and not isinstance(c["for"], str):
        return "bad target"
    return None


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def as_url(text: str) -> str | None:
    """The text when it is a single http(s) URL (and nothing else), else None."""
    t = text.strip()
    return t if URL.match(t) and urlsplit(t).netloc else None


def short_url(url: str, limit: int = 60) -> str:
    """Host and path only (no scheme, query or fragment), shortened for a notification."""
    u = urlsplit(url)
    s = (u.netloc + u.path).rstrip("/") or url
    return s if len(s) <= limit else s[: limit - 1] + "…"


# ---------------------------------------------------------------------- the watcher's logic (pure, behind an interface)

@dataclass
class Flags:
    """What the clipboard says about itself, read before its text."""
    exclude: bool = False  # ExcludeClipboardContentFromMonitorProcessing is present
    history: int | None = None  # CanIncludeInClipboardHistory DWORD, if present
    cloud: int | None = None  # CanUploadToCloudClipboard DWORD, if present

    @property
    def excluded(self) -> bool:
        return self.exclude or self.history == 0 or self.cloud == 0


class ClipSource(Protocol):
    """The platform's clipboard (clipwin.WinClipboard on Windows). Reads retry on their own; None means unreadable."""

    def start(self, on_update: Callable[[], None]) -> None: ...
    def stop(self) -> None: ...
    def flags(self) -> Flags | None: ...
    def text(self) -> str | None: ...
    def write(self, text: str, exclude: bool = True) -> None: ...


class ClipWatcher:
    """A clipboard update comes in (any thread); the last of a burst, 300 ms later, is checked and handed to `sink`."""

    def __init__(self, source: ClipSource, sink: Callable[[str], None], enabled: Callable[[], bool] = lambda: True,
                 debounce: float = DEBOUNCE):
        self.source, self.sink, self.enabled, self.debounce = source, sink, enabled, debounce
        self.written: str | None = None  # sha256 of the text this device last put on the clipboard (echo guard)
        self.last_sent: str | None = None  # sha256 of the last text handed to the sink
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        self.source.start(self.update)

    def stop(self) -> None:
        self.source.stop()
        with self._lock:
            if self._timer:
                self._timer.cancel()
                self._timer = None

    def update(self) -> None:
        if not self.enabled():
            return
        with self._lock:
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(self.debounce, self.process)
            self._timer.daemon = True
            self._timer.start()

    def process(self) -> str | None:
        """Decide about the clipboard as it is now. Returns the text handed on, else None (the reason is not kept)."""
        if not self.enabled():
            return None
        flags = self.source.flags()
        if flags is None or flags.excluded:  # a password manager, or an app that asked not to be synced
            return None
        text = self.source.text()  # only now is the text read
        if not text or len(text.encode("utf-8")) > MAX_BYTES:
            return None
        h = digest(text)
        if h == self.written or h == self.last_sent:  # what we wrote ourselves, or the same copy again
            return None
        self.last_sent = h
        self.sink(text)
        return text

    def forget_sent(self, text: str) -> None:
        """A send failed: copying that same text again must try again."""
        if self.last_sent == digest(text):
            self.last_sent = None


# ---------------------------------------------------------------------- the hub: one per agent

@dataclass
class Pending:
    clip: dict
    expires: float


class ClipSync:
    def __init__(self, agent: SendAgent, clock: Callable[[], float] = time.monotonic):
        self.agent = agent
        self.clock = clock
        c = agent.cfg.clip
        self.peer = c.peer or ("pc" if agent.cfg.device_id == "phone" else "phone")
        self.enabled = c.enabled
        self._file = agent.data / "clip.json"
        with contextlib.suppress(OSError, ValueError, AttributeError):
            self.enabled = bool(json.loads(self._file.read_text(encoding="utf-8"))["enabled"])
        self.seen: deque[str] = deque(maxlen=SEEN_KEEP)
        self.newest: dict[str, int] = {}  # sender -> newest applied `at`
        self.pending: dict[str, Pending] = {}  # holder only: target -> latest clip, RAM only
        self.source: ClipSource | None = None
        self.watcher: ClipWatcher | None = None
        self.on_link: Callable[[str, str], None] = lambda url, origin: None  # (url, origin device id): the toast
        self._pushing = False
        self._wake: asyncio.Event | None = None

    # -------------------------------------------------- the switch
    def set_enabled(self, on: bool) -> None:
        self.enabled = on
        tmp = self._file.with_suffix(".tmp")
        self._file.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps({"enabled": on}), encoding="utf-8")  # only the switch, never a clip
        os.replace(tmp, self._file)
        if not on:
            self.pending.clear()
        self.sync_watcher()

    def state(self) -> dict:
        return {"enabled": self.enabled, "peer": self.peer, "peer_name": self.agent.devices.name_of(self.peer),
                "watching": bool(self.watcher and self.enabled), "pending": len(self.pending)}

    # -------------------------------------------------- the PC watcher
    def attach(self, source: ClipSource) -> None:
        self.source = source
        self.watcher = ClipWatcher(source, self._copied, lambda: self.enabled)

    def sync_watcher(self) -> None:
        """The listener thread runs only while sync is on."""
        if not self.watcher:
            return
        if self.enabled:
            self.watcher.start()
        else:
            self.watcher.stop()

    def _copied(self, text: str) -> None:
        loop = self.agent._loop
        if not loop or loop.is_closed():
            return
        fut = asyncio.run_coroutine_threadsafe(self.send("copy", text), loop)
        fut.add_done_callback(lambda f: self._done(f, text))

    def _done(self, fut, text: str) -> None:
        if fut.cancelled() or fut.exception():
            e = None if fut.cancelled() else fut.exception()
            if self.watcher:
                self.watcher.forget_sent(text)
            log.info("clip not sent: %s", e.__class__.__name__ if e else "cancelled")  # never the text

    # -------------------------------------------------- sending
    def make(self, kind: str, text: str, target: str | None = None) -> dict:
        return {"id": str(uuid.uuid4()), "kind": kind, "text": text, "at": int(time.time() * 1000),
                "from": self.agent.cfg.device_id, "for": target or self.peer}

    async def send(self, kind: str, text: str) -> str:
        """Send a clip to the peer: straight if it answers within 3 s, else through the holder. Returns the route."""
        if not self.enabled:
            raise ClipDisabled("clipboard sync is off")
        if kind not in KINDS or not text or len(text.encode("utf-8")) > MAX_BYTES:
            raise ClipError("nothing to send")
        clip = self.make(kind, text)
        link = await self._probe(self.peer)
        if link is not None:
            try:
                await self._post(link, self.peer, clip)
                log.info("clip sent to %s (%d chars)", self.peer, len(text))
                return "direct"
            except ClipDisabled:
                raise
            except (ClipError, httpx.HTTPError, OSError):
                pass  # it answered hello but not the clip: try the holder
        holder = self.agent.cfg.holder
        if holder and holder != self.peer:
            hl = await self._probe(holder)
            if hl is not None:
                try:
                    await self._post(hl, holder, clip)
                    log.info("clip sent to %s (%d chars)", holder, len(text))
                    return "holder"
                except (ClipError, httpx.HTTPError, OSError):
                    pass
        raise ClipError(f"{self.agent.devices.name_of(self.peer)} is not reachable")

    async def _probe(self, target: str) -> Link | None:
        try:
            return await asyncio.wait_for(self.agent.sender.probe(target), PROBE_BUDGET)
        except (TimeoutError, Refused, httpx.HTTPError, OSError):
            return None

    async def _post(self, link: Link, target: str, clip: dict) -> None:
        """One POST /send/clip, sealed with this link's key (a link that expired meanwhile is made again first)."""
        sender = self.agent.sender
        for attempt in (0, 1):
            if link.expires < time.time() or attempt:
                link = await sender.hello(target, link.kind, link.base)
            body = seal(link.key, clip)
            r = await sender.call(link, "POST", "/send/clip", body=body, target=target, _retry=False)
            if r.status_code == 200:
                return
            if r.status_code == 409:
                raise ClipDisabled("clipboard sync is off on " + self.agent.devices.name_of(target))
            if r.status_code != 401 or attempt:  # 401: their session ended, hello again once
                raise ClipError(f"{target} answered {r.status_code}")

    # -------------------------------------------------- receiving
    def receive(self, clip: dict, peer: str) -> dict:
        """A clip arrived from `peer` (already authenticated and decrypted). Raises ClipError for a refusal,
        returns {"ok": True, ...}. A copy or link for this device is applied by the caller (`applied`)."""
        why = check_clip(clip)
        if why:
            raise ClipError(why)
        me = self.agent.cfg.device_id
        origin = clip["from"]
        if origin != peer and peer != self.agent.cfg.holder:  # `from` is trusted only from the holder
            raise ClipError("clip from someone else")
        target = clip.get("for")
        if self.agent.cfg.holding and origin == peer and target and target != me:  # a relay request
            if not self.agent.devices.get(target):
                raise ClipError("unknown target")
            self.pending[target] = Pending(clip, self.clock() + PENDING_TTL)
            self.kick()
            return {"ok": True, "held": True}
        if self.agent.cfg.holding or (target and target != me):
            raise ClipError("not for this device")
        if clip["id"] in self.seen:
            return {"ok": True, "dup": True}
        newest = self.newest.get(origin)
        if newest is not None and clip["at"] < newest - SKEW_MS:
            return {"ok": True, "stale": True}
        self.seen.append(clip["id"])
        self.newest[origin] = max(clip["at"], newest or 0)
        return {"ok": True, "apply": True}

    def apply(self, clip: dict) -> None:
        """Put a received clip to use: a copy goes on the clipboard (not echoed back), a link becomes a notification."""
        if clip["kind"] == "link":
            if as_url(clip["text"]):
                self.on_link(clip["text"].strip(), clip["from"])
            return
        if self.watcher:
            self.watcher.written = digest(clip["text"])
        if self.source:
            self.source.write(clip["text"], exclude=True)

    # -------------------------------------------------- the holder's pending clips
    def expire(self) -> None:
        now = self.clock()
        for t in [t for t, p in self.pending.items() if p.expires <= now]:
            del self.pending[t]

    def kick(self) -> None:
        """Something is pending, or a device woke up: push now (on the agent's loop; callable from any thread)."""
        loop = self.agent._loop
        if self.pending and loop and not loop.is_closed():
            loop.call_soon_threadsafe(self._kick_now)

    def _kick_now(self) -> None:
        if self._wake:
            self._wake.set()
        asyncio.ensure_future(self.push())

    async def push(self) -> int:
        """Try every pending target once; delivered and expired clips are dropped. Returns how many were delivered."""
        if self._pushing:
            return 0
        self._pushing = True
        n = 0
        try:
            self.expire()
            for target, p in list(self.pending.items()):
                link = await self._probe(target)
                if link is None:
                    continue
                try:
                    await self._post(link, target, p.clip)  # re-encrypted with the target's session key
                except ClipDisabled:
                    pass  # they have sync off: drop it
                except (ClipError, httpx.HTTPError, OSError):
                    continue
                if self.pending.get(target) is p:  # a newer clip may have replaced it meanwhile
                    del self.pending[target]
                n += 1
                log.info("clip delivered to %s (%d chars)", target, len(p.clip["text"]))
        finally:
            self._pushing = False
        return n

    async def loop(self) -> None:
        """Every 30 s, but only while something is pending (otherwise this just waits)."""
        self._wake = asyncio.Event()
        while True:
            if not self.pending:
                await self._wake.wait()
            self._wake.clear()
            if self.pending:
                await asyncio.sleep(PENDING_EVERY)
                await self.push()
