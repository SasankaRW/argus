"""The receiving side: a small FastAPI app on the agent's port (default 8610).

hello -> session (Ed25519 proof + HKDF key) -> every request carries X-Send-Session, X-Send-Proof and a MAC.
Chunks land in <root>/.incoming/<offer>/<n>.part; /done verifies sha256 and moves files into
<root>/From <sender>/ without ever overwriting or deleting a user's file.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from . import CHUNK, SESSION_SECONDS
from . import clip as clipmod
from .keys import Ephemeral, b64, request_mac, session_key, unb64, verify

if TYPE_CHECKING:
    from .agent import SendAgent

OFFER_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")
CLIP_LOCAL = ("/send/clip", "/send/clip/enabled", "/send/clip/link")  # asked by this device's own island or Helios
BAD_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class FileSpec(BaseModel):
    n: int = Field(..., ge=0)
    rel_path: str = Field(..., min_length=1, max_length=1000)
    size: int = Field(..., ge=0)
    mtime: float = 0
    sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")


class Offer(BaseModel):
    offer_id: str
    # `from` is a Python keyword; the wire name is kept with an alias
    sender: str = Field(..., alias="from")
    files: list[FileSpec]
    total: int = Field(..., ge=0)
    chunk: int = Field(CHUNK, ge=1024, le=CHUNK)  # the sender's chunk size (8 MiB unless a test says otherwise)
    hold_for: str | None = None
    origin: str | None = None  # the original sender, when a holding agent delivers

    model_config = {"populate_by_name": True}


class Enabled(BaseModel):
    enabled: bool


class LinkIn(BaseModel):
    url: str = Field(..., max_length=2000)


class Hello(BaseModel):
    sender: str = Field(..., alias="from")
    nonce_c: str
    eph_c: str = ""

    model_config = {"populate_by_name": True}


@dataclass
class Session:
    peer: str
    key: bytes
    nonce_s: bytes
    eph_c: bytes
    expires: float
    proven: bool = False
    seen: set = field(default_factory=set)


@dataclass
class Receiver:
    agent: SendAgent
    sessions: dict[str, Session] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)
    parsed: dict = field(default_factory=dict)  # offer id -> (dir, offer): offer.json is read once, not per chunk
    got: dict = field(default_factory=dict)  # offer id -> bytes held so far (live progress; rebuilt on resume)

    # ------------------------------------------------------------------ paths
    @property
    def root(self) -> Path:
        return self.agent.root

    def offer_dir(self, offer_id: str, hold: bool = False) -> Path:
        return self.root / (".holding" if hold else ".incoming") / offer_id

    def find_offer(self, offer_id: str) -> tuple[Path, dict] | None:
        hit = self.parsed.get(offer_id)
        if hit and (hit[0] / "offer.json").exists():
            return hit
        self.parsed.pop(offer_id, None)
        for hold in (False, True):
            d = self.offer_dir(offer_id, hold)
            if (d / "offer.json").exists():
                self.parsed[offer_id] = (d, json.loads((d / "offer.json").read_text(encoding="utf-8")))
                return self.parsed[offer_id]
        return None

    def free_bytes(self) -> int:
        self.root.mkdir(parents=True, exist_ok=True)
        return shutil.disk_usage(self.root).free

    def holding_used(self) -> int:
        base = self.root / ".holding"
        if not base.exists():
            return 0
        return sum(f.stat().st_size for f in base.rglob("*") if f.is_file())

    # ------------------------------------------------------------------ state of one offer
    @staticmethod
    def chunks_of(d: Path, n: int) -> list[int]:
        p = d / f"{n}.chunks"
        if not p.exists():
            return []
        return sorted({int(x) for x in p.read_text().split() if x.isdigit()})

    @staticmethod
    def n_chunks(size: int, chunk: int) -> int:
        return -(-size // chunk)

    def existing_same(self, dest_dir: Path, rel: PurePosixPath, size: int, sha: str) -> Path | None:
        """A file already in the final folder with this sha256 (under the name or a 'name (2)' style one)."""
        for cand in self._names(dest_dir, rel):
            if not cand.exists():
                return None
            if cand.is_file() and cand.stat().st_size == size and _sha256_file(cand) == sha:
                return cand
        return None

    @staticmethod
    def _names(dest_dir: Path, rel: PurePosixPath):
        base = dest_dir.joinpath(*rel.parts)
        yield base
        for k in range(2, 10000):
            yield base.with_name(f"{rel.stem} ({k}){rel.suffix}")

    def final_dir(self, sender: str, origin: str | None) -> Path:
        name = BAD_NAME.sub("_", self.agent.devices.name_of(origin or sender)).strip(" .") or "device"
        return self.root / f"From {name}"

    def reply(self, d: Path, offer: dict) -> dict:
        have, done = {}, []
        dest = self.final_dir(offer["from"], offer.get("origin"))
        for f in offer["files"]:
            if offer.get("hold_for"):
                if (d / f"{f['n']}.data").exists():
                    done.append(f["n"])
                    continue
            elif self.existing_same(dest, safe_rel(f["rel_path"]), f["size"], f["sha256"]):
                done.append(f["n"])
                continue
            have[f["n"]] = self.chunks_of(d, f["n"])
        return {"have": have, "done": done}

    # ------------------------------------------------------------------ auth
    def check(self, request: Request, body_sha: str) -> Session:
        sid = request.headers.get("x-send-session", "")
        mac = request.headers.get("x-send-mac", "")
        s = self.sessions.get(sid)
        if not s or s.expires < time.time():
            raise HTTPException(401, "no session: say hello first")
        if not s.proven:
            dev = self.agent.devices.get(s.peer)
            proof = request.headers.get("x-send-proof", "")
            if not dev or not proof or not verify(dev["pubkey"], s.nonce_s + s.eph_c, proof):
                raise HTTPException(403, "key not recognised")
            s.proven = True
        seq = request.headers.get("x-send-seq", "")
        want = request_mac(s.key, request.method, request.url.path, request.url.query, body_sha, seq)
        if not mac or not hmac.compare_digest(mac, want):
            raise HTTPException(401, "bad MAC")
        if not seq.isdigit() or int(seq) < 1 or seq in s.seen:
            raise HTTPException(401, "replayed or missing sequence number")
        s.seen.add(seq)
        return s


def safe_rel(rel: str) -> PurePosixPath:
    parts = [BAD_NAME.sub("_", p) for p in rel.replace("\\", "/").split("/") if p not in ("", ".", "..")]
    parts = [p.rstrip(" .") or "_" for p in parts]
    if not parts:
        raise HTTPException(400, "bad path")
    return PurePosixPath(*parts)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def open_on_this_device(path: Path, reveal: bool) -> None:
    """Open a file in its usual app on this machine, or show it selected in its folder."""
    if sys.platform == "win32":
        if reveal:
            subprocess.Popen(["explorer", f"/select,{path}"])  # noqa: S603,S607
        else:
            os.startfile(path)  # type: ignore[attr-defined]  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path)] if reveal else ["open", str(path)])  # noqa: S603,S607
    else:
        subprocess.Popen(["xdg-open", str(path.parent if reveal else path)])  # noqa: S603,S607


def build_app(agent: SendAgent) -> FastAPI:
    rx = Receiver(agent)
    app = FastAPI(title="Argus Send", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.receiver = rx
    app.state.hits = []  # (method, path) of every authenticated request: tests read it

    @app.exception_handler(HTTPException)
    async def _http(request: Request, exc: HTTPException):
        body = exc.detail if isinstance(exc.detail, dict) else {"detail": exc.detail}
        return JSONResponse(body, status_code=exc.status_code)

    # ---- my transfers, for Helios on this same device: list what this device sent and received, open a file in its
    # own app or show it in its folder. Asked from the browser on this machine (127.0.0.1), never through Argus, so it
    # works while Argus is down. The worker token is required, and only Helios's own address may ask from a page.
    helios_origin = ""
    if agent.cfg.argus_url:
        u = urlsplit(agent.cfg.argus_url)
        helios_origin = f"{u.scheme}://{u.netloc}"

    @app.middleware("http")
    async def helios_cors(request: Request, call_next):
        if not (request.url.path.startswith("/send/mine") or request.url.path in CLIP_LOCAL):
            return await call_next(request)
        origin = request.headers.get("origin", "")
        allow = bool(origin) and origin in (helios_origin, "http://localhost:5173")
        if request.method == "OPTIONS":  # the browser's preflight (and Chrome's local-network check)
            if not allow:
                return Response(status_code=403)
            return Response(status_code=204, headers={
                "Access-Control-Allow-Origin": origin, "Access-Control-Allow-Methods": "GET, POST",
                "Access-Control-Allow-Headers": "Authorization, Content-Type",
                "Access-Control-Allow-Private-Network": "true",
                "Access-Control-Max-Age": "600", "Vary": "Origin"})
        if origin and not allow:
            return JSONResponse({"detail": "not from Helios"}, status_code=403)
        resp = await call_next(request)
        if origin:
            resp.headers["Access-Control-Allow-Origin"] = origin
            resp.headers["Vary"] = "Origin"
        return resp

    def token_only(request: Request) -> None:
        want = agent.argus_token or ""
        got = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not want or not hmac.compare_digest(got.encode(), want.encode()):
            raise HTTPException(401, "the worker token is needed")

    def shown_name(p: Path, kind: str) -> str:
        """"photos/a.jpg" for a received folder (inside "From <device>/"), else the file's own name."""
        if kind == "received":
            try:
                return PurePosixPath(*p.relative_to(rx.root).parts[1:]).as_posix() or p.name
            except ValueError:
                pass
        return p.name

    @app.get("/send/mine")
    async def mine(request: Request) -> dict:
        token_only(request)

        def build() -> list[dict]:
            out = []
            for e in agent.mine():
                files = []
                for n, f in enumerate(e["files"]):
                    p = Path(f)
                    there = p.is_file()
                    files.append({"n": n, "name": shown_name(p, e["kind"]), "size": p.stat().st_size if there else 0,
                                  "here": there})
                out.append({k: e[k] for k in ("kind", "offer_id", "at", "label", "peer")} | {"files": files})
            return out

        return {"device": agent.cfg.device_id, "items": await asyncio.to_thread(build)}

    def mine_file(kind: str, offer_id: str, n: int) -> Path:
        files = agent.mine_files(kind, offer_id) if kind in ("sent", "received") and OFFER_ID.match(offer_id) else None
        if files is None or not 0 <= n < len(files):
            raise HTTPException(404, "not one of this device's transfers")
        p = Path(files[n])
        if not p.is_file():
            raise HTTPException(410, "that file was moved or deleted")
        return p

    @app.post("/send/mine/{kind}/{offer_id}/{n}/open")
    async def open_mine(kind: str, offer_id: str, n: int, request: Request) -> dict:
        token_only(request)
        await asyncio.to_thread(open_on_this_device, mine_file(kind, offer_id, n), False)
        return {"ok": True}

    @app.post("/send/mine/{kind}/{offer_id}/{n}/reveal")
    async def reveal_mine(kind: str, offer_id: str, n: int, request: Request) -> dict:
        token_only(request)
        await asyncio.to_thread(open_on_this_device, mine_file(kind, offer_id, n), True)
        return {"ok": True}

    @app.post("/send/hello")
    async def hello(body: Hello) -> dict:
        dev = agent.devices.get(body.sender)
        if not dev:
            raise HTTPException(403, "unknown device")
        try:
            nonce_c = unb64(body.nonce_c)
            eph_c = unb64(body.eph_c)
            if len(eph_c) != 32:
                raise ValueError("eph")
        except ValueError:
            raise HTTPException(400, "bad nonce") from None
        agent.clip.kick()  # a device that says hello may be waiting for a clip held here
        nonce_s = os.urandom(32)
        sid = secrets.token_urlsafe(16)
        now = time.time()
        for k in [k for k, s in rx.sessions.items() if s.expires < now]:
            del rx.sessions[k]
        eph = Ephemeral()
        key = session_key(eph.shared(eph_c), nonce_c, nonce_s, dev["pubkey"], agent.key.pub)
        rx.sessions[sid] = Session(body.sender, key, nonce_s, eph_c, now + SESSION_SECONDS)
        return {"sid": sid, "nonce_s": b64(nonce_s), "eph_s": b64(eph.pub), "sig": agent.key.sign(nonce_c + eph.pub)}

    def note(request: Request) -> None:
        app.state.hits.append((request.method, request.url.path, request.url.query))

    def live(offer: dict, done: int, state: str = "moving") -> None:
        """Incoming progress (the second leg of a held share is "hold"): for Argus and the notification."""
        files = offer["files"]
        first = files[0]["rel_path"].split("/")[0] if files else ""
        label = first if len({f["rel_path"].split("/")[0] for f in files}) == 1 else f"{len(files)} files"
        relayed = offer.get("origin") and offer["origin"] != offer["from"]
        agent.progress(offer["offer_id"], "hold" if relayed else "send", src=offer["from"], dst=agent.cfg.device_id,
                       label=label, files=len(files), total=offer["total"], done=done, state=state)

    @app.post("/send/offers")
    async def make_offer(request: Request) -> dict:
        raw = await request.body()
        s = rx.check(request, hashlib.sha256(raw).hexdigest())
        note(request)
        offer = Offer.model_validate_json(raw)
        if not OFFER_ID.match(offer.offer_id):
            raise HTTPException(400, "bad offer id")
        if offer.sender != s.peer:
            raise HTTPException(403, "offer from someone else")
        hold = offer.hold_for is not None
        if hold and not agent.cfg.holding:
            raise HTTPException(403, "this device does not hold shares")
        d = rx.offer_dir(offer.offer_id, hold)
        meta = offer.model_dump(by_alias=True)
        have_before = sum(len(rx.chunks_of(d, f.n)) * offer.chunk for f in offer.files) if d.exists() else 0
        need = max(0, offer.total - have_before)
        if hold:
            cap = int(agent.cfg.holding_max_gb * 1024**3)
            if rx.holding_used() + need > cap:
                over = rx.holding_used() + need - cap
                raise HTTPException(507, {"detail": "holding area is full", "missing_bytes": over})
        free = await asyncio.to_thread(rx.free_bytes)
        if free < need:
            raise HTTPException(507, {"detail": "not enough free space", "missing_bytes": need - free})
        d.mkdir(parents=True, exist_ok=True)
        meta["created"] = meta.get("created") or time.time()
        if (d / "offer.json").exists():
            meta["created"] = json.loads((d / "offer.json").read_text(encoding="utf-8")).get("created", meta["created"])
        (d / "offer.json").write_text(json.dumps(meta), encoding="utf-8")
        rx.parsed.pop(offer.offer_id, None)
        return await asyncio.to_thread(rx.reply, d, meta)

    @app.get("/send/offers/{offer_id}")
    async def get_offer(offer_id: str, request: Request) -> dict:
        rx.check(request, hashlib.sha256(b"").hexdigest())
        note(request)
        found = rx.find_offer(offer_id) if OFFER_ID.match(offer_id) else None
        if not found:
            raise HTTPException(404, "no such offer")
        return await asyncio.to_thread(rx.reply, *found)

    @app.put("/send/offers/{offer_id}/files/{n}")
    async def put_chunk(offer_id: str, n: int, chunk: int, request: Request) -> dict:
        claimed = request.headers.get("x-chunk-sha256", "")
        rx.check(request, claimed)  # the MAC covers the chunk's sha256
        note(request)
        found = rx.find_offer(offer_id) if OFFER_ID.match(offer_id) else None
        if not found:
            raise HTTPException(404, "no such offer")
        d, offer = found
        spec = next((f for f in offer["files"] if f["n"] == n), None)
        size_chunk = offer["chunk"]
        if spec is None or not 0 <= chunk < rx.n_chunks(spec["size"], size_chunk):
            raise HTTPException(400, "no such file or chunk")
        body = await request.body()
        want_len = min(size_chunk, spec["size"] - chunk * size_chunk)
        if hashlib.sha256(body).hexdigest() != claimed or len(body) != want_len:
            raise HTTPException(422, "chunk is damaged, send it again")

        def write() -> None:
            part = d / f"{n}.part"
            with rx.lock:
                mode = "r+b" if part.exists() else "w+b"
                with open(part, mode) as f:
                    f.seek(chunk * size_chunk)
                    f.write(body)
                with open(d / f"{n}.chunks", "a") as f:
                    f.write(f"{chunk}\n")

        await asyncio.to_thread(write)
        if offer_id not in rx.got:  # first chunk since start (or a resume): count what is already here
            rx.got[offer_id] = sum(min(f["size"], len(rx.chunks_of(d, f["n"])) * size_chunk) for f in offer["files"])
        else:
            rx.got[offer_id] += len(body)
        live(offer, rx.got[offer_id])
        return {"ok": True}

    @app.post("/send/offers/{offer_id}/done")
    async def done(offer_id: str, request: Request) -> JSONResponse:
        rx.check(request, hashlib.sha256(await request.body()).hexdigest())
        note(request)
        found = rx.find_offer(offer_id) if OFFER_ID.match(offer_id) else None
        if not found:
            raise HTTPException(404, "no such offer")
        status, out = await asyncio.to_thread(finish, rx, *found)
        if status == 200:
            live(found[1], found[1]["total"], "done")
            rx.got.pop(offer_id, None)
        return JSONResponse(out, status_code=status)

    @app.post("/send/offers/{offer_id}/delivered")
    async def delivered(offer_id: str, request: Request) -> dict:
        s = rx.check(request, hashlib.sha256(await request.body()).hexdigest())
        note(request)
        if not agent.mark_delivered(offer_id, s.peer):
            raise HTTPException(404, "no such held share")
        return {"ok": True}

    @app.delete("/send/holding/{offer_id}")
    async def drop_holding(offer_id: str, request: Request) -> dict:
        rx.check(request, hashlib.sha256(b"").hexdigest())
        note(request)
        if not OFFER_ID.match(offer_id):
            raise HTTPException(400, "bad offer id")
        shutil.rmtree(rx.offer_dir(offer_id, True), ignore_errors=True)
        agent.cancel_hold(offer_id)
        return {"ok": True}

    # ---- clipboard and link sync. POST /send/clip is the peers' route (session MAC like an offer, encrypted body);
    # the other three are this device's own island/CLI, worker token only. Clip text is never logged or kept.
    @app.post("/send/clip")
    async def receive_clip(request: Request) -> dict:
        raw = await request.body()
        s = rx.check(request, hashlib.sha256(raw).hexdigest())
        note(request)
        clip = agent.clip
        if not clip.enabled:
            raise HTTPException(409, "clipboard sync is off")
        try:
            got = clipmod.unseal(s.key, raw)
            out = clip.receive(got, s.peer)
        except (ValueError, clipmod.ClipError) as e:
            raise HTTPException(400, str(e)) from None
        if out.pop("apply", False):
            try:
                await asyncio.to_thread(clip.apply, got)
            except OSError:
                raise HTTPException(503, "the clipboard is busy") from None
        return out

    @app.get("/send/clip")
    async def clip_state(request: Request) -> dict:
        token_only(request)
        return agent.clip.state()

    @app.post("/send/clip/enabled")
    async def clip_enabled(body: Enabled, request: Request) -> dict:
        token_only(request)
        await asyncio.to_thread(agent.clip.set_enabled, body.enabled)
        return agent.clip.state()

    @app.post("/send/clip/link")
    async def clip_link(body: LinkIn, request: Request) -> dict:
        token_only(request)
        url = clipmod.as_url(body.url)
        if not url:
            raise HTTPException(400, "that is not a web link")
        try:
            return {"ok": True, "via": await agent.clip.send("link", url)}
        except clipmod.ClipDisabled as e:
            raise HTTPException(409, str(e)) from None
        except clipmod.ClipError as e:
            raise HTTPException(502, str(e)) from None

    return app


def finish(rx: Receiver, d: Path, offer: dict) -> tuple[int, dict]:
    """Verify every file; if all are good move them (or, in the holding area, keep them) and say where."""
    hold = offer.get("hold_for") is not None
    dest_dir = rx.final_dir(offer["from"], offer.get("origin"))
    chunk = offer["chunk"]
    bad: dict[int, list[int]] = {}
    already: dict[int, Path] = {}
    for f in offer["files"]:
        n, size = f["n"], f["size"]
        if hold and (d / f"{n}.data").exists():
            continue
        same = None if hold else rx.existing_same(dest_dir, safe_rel(f["rel_path"]), size, f["sha256"])
        if same:
            already[n] = same
            continue
        count = rx.n_chunks(size, chunk)
        got = set(rx.chunks_of(d, n))
        missing = [i for i in range(count) if i not in got]
        if missing:
            bad[n] = missing
            continue
        part = d / f"{n}.part"
        if size == 0 and not part.exists():
            part.touch()
        if part.stat().st_size != size or _sha256_file(part) != f["sha256"]:
            bad[n] = list(range(count))
            (d / f"{n}.chunks").unlink(missing_ok=True)  # start that file again
    if bad:
        return 409, {"detail": "some files did not verify", "bad": {str(k): v for k, v in bad.items()}}
    saved: list[str] = []
    for f in offer["files"]:
        n = f["n"]
        if n in already:
            saved.append(str(already[n]))
            continue
        part, data = d / f"{n}.part", d / f"{n}.data"
        if hold:
            if part.exists():
                os.replace(part, data)
            continue
        rel = safe_rel(f["rel_path"])
        os.utime(part, (f["mtime"] or time.time(),) * 2)
        for cand in rx._names(dest_dir, rel):
            cand.parent.mkdir(parents=True, exist_ok=True)
            try:
                fd = os.open(cand, os.O_CREAT | os.O_EXCL | os.O_WRONLY)  # claim the name; never overwrite
            except FileExistsError:
                continue
            os.close(fd)
            os.replace(part, cand)
            saved.append(str(cand))
            break
    if hold:
        meta = json.loads((d / "offer.json").read_text(encoding="utf-8"))
        meta["complete"] = True
        (d / "offer.json").write_text(json.dumps(meta), encoding="utf-8")
        rx.parsed.pop(offer["offer_id"], None)
        rx.agent.accept_hold(offer)
        return 200, {"saved": [], "held": True}
    shutil.rmtree(d, ignore_errors=True)
    rx.agent.received(offer, saved)
    return 200, {"saved": saved}
