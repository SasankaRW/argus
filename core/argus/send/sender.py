"""The sending side: pick a path (LAN, else Tailscale), offer, push chunks in parallel, verify, finish.

Everything the receiver already has is skipped, so a broken transfer resumes where it stopped. While on
Tailscale the LAN is tried again every few seconds; the remaining chunks move over as soon as it answers.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

from . import PORT, SESSION_SECONDS, SMALL
from .keys import Ephemeral, b64, request_mac, session_key, unb64, verify

if TYPE_CHECKING:
    from .agent import SendAgent

ATTEMPTS = 5


class SendError(Exception):
    """Something that may go away: the item stays queued and is tried again."""


class NoSpace(SendError):
    pass


class Refused(Exception):
    """The target does not accept us (unknown key): not retried."""


@dataclass
class Link:
    kind: str  # "lan" or "tailscale"
    base: str
    sid: str
    key: bytes
    proof: str
    expires: float
    seq: int = 0


@dataclass
class Run:
    item: dict
    target: str
    cur: Link
    sent: int = 0
    shown: float = 0.0


class Cancelled(Exception):  # noqa: N818  the share was cancelled while it was moving
    pass

log = logging.getLogger("argus.send")


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def read_range(path: str, start: int, length: int) -> bytes:
    with open(path, "rb") as f:
        f.seek(start)
        return f.read(length)


def label_of(manifest: list[dict]) -> str:
    first = manifest[0]["rel_path"].split("/")[0] if manifest else ""
    return first if len({f["rel_path"].split("/")[0] for f in manifest}) == 1 else f"{len(manifest)} files"


class Sender:
    def __init__(self, agent: SendAgent, transport: httpx.AsyncBaseTransport | None = None):
        self.agent = agent
        self.cfg = agent.cfg
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self.links: dict[str, Link] = {}

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(transport=self._transport, timeout=60)
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()

    # ------------------------------------------------------------------ paths and sessions
    def candidates(self, target: str, kind: str | None = None) -> list[tuple[str, str]]:
        d = self.agent.devices.get(target) or {}
        out: list[tuple[str, str]] = []
        if kind in (None, "lan"):
            addrs = list(d.get("lan") or []) + self.agent.discovery.addresses(target)
            out += [("lan", "http://" + a) for a in dict.fromkeys(addrs)]
        if kind in (None, "tailscale") and d.get("tailscale"):
            host = d["tailscale"]
            out.append(("tailscale", "http://" + (host if ":" in host else f"{host}:{PORT}")))
        return out

    async def hello(self, target: str, kind: str, base: str) -> Link:
        dev = self.agent.devices.get(target)
        if not dev:
            raise Refused("unknown device")
        nonce_c = os.urandom(32)
        eph = Ephemeral()
        r = await self.client.post(base + "/send/hello", timeout=self.cfg.probe_timeout_seconds,
                                   json={"from": self.cfg.device_id, "nonce_c": b64(nonce_c), "eph_c": b64(eph.pub)})
        if r.status_code == 403:
            raise Refused("the other device does not know this one")
        r.raise_for_status()
        d = r.json()
        eph_s = unb64(d["eph_s"])
        if not verify(dev["pubkey"], nonce_c + eph_s, d["sig"]):
            raise Refused("the other device's key does not match")
        nonce_s = unb64(d["nonce_s"])
        key = session_key(eph.shared(eph_s), nonce_c, nonce_s, self.agent.key.pub, dev["pubkey"])
        link = Link(kind, base, d["sid"], key, self.agent.key.sign(nonce_s + eph.pub),
                    time.time() + SESSION_SECONDS - 30)
        self.links[base] = link
        return link

    async def probe(self, target: str, kind: str | None = None) -> Link | None:
        """Hello on every address at once; the first good LAN answer wins, else Tailscale."""
        cands = self.candidates(target, kind)
        if not cands:
            return None
        res = await asyncio.gather(*(self.hello(target, k, b) for k, b in cands), return_exceptions=True)
        good = [r for r in res if isinstance(r, Link)]
        if not good and any(isinstance(r, Refused) for r in res):
            raise next(r for r in res if isinstance(r, Refused))
        return next((g for g in good if g.kind == "lan"), good[0] if good else None)

    async def call(self, link: Link, method: str, path: str, *, query: str = "", body: bytes = b"",
                   chunk_sha: str | None = None, target: str = "", _retry: bool = True) -> httpx.Response:
        if link.expires < time.time():
            link = await self.hello(target, link.kind, link.base)
        sha = chunk_sha or hashlib.sha256(body).hexdigest()
        link.seq += 1
        seq = link.seq
        headers = {"X-Send-Session": link.sid, "X-Send-Proof": link.proof, "X-Send-Seq": str(seq),
                   "X-Send-Mac": request_mac(link.key, method, path, query, sha, seq)}
        if chunk_sha:
            headers["X-Chunk-Sha256"] = chunk_sha
        if body and not chunk_sha:
            headers["Content-Type"] = "application/json"
        url = link.base + path + (f"?{query}" if query else "")
        r = await self.client.request(method, url, content=body or None, headers=headers)
        if r.status_code == 401 and _retry:  # session expired on their side
            link = await self.hello(target, link.kind, link.base)
            return await self.call(link, method, path, query=query, body=body, chunk_sha=chunk_sha, target=target,
                                   _retry=False)
        return r

    # ------------------------------------------------------------------ one queue entry
    async def process(self, item: dict) -> None:
        store, oid, target = self.agent.store, item["id"], item["target"]
        manifest = item["manifest"]
        age = time.time() - item["created"]
        if item["state"] == "queued" and not item["asked"] and age > self.cfg.keep_waiting_days * 86400:
            store.set(oid, asked=1)
            store.event("keep_waiting", peer=self.agent.devices.name_of(target), label=label_of(manifest),
                        detail="still waiting; keep waiting?", offer_id=oid)
        try:
            for f in manifest:
                st = await asyncio.to_thread(os.stat, f["abs"]) if os.path.exists(f["abs"]) else None
                moved = st is not None and item["kind"] == "send" and int(st.st_mtime) != int(f["mtime"])
                if st is None or st.st_size != f["size"] or moved:
                    return self._fail(item, f"{f['rel_path']} changed or was deleted; not sent")
            todo = [f for f in manifest if not f.get("sha256")]
            if todo:
                for f in todo:
                    f["sha256"] = await asyncio.to_thread(sha256_file, f["abs"])
                store.set(oid, manifest=manifest)
            link = await self.probe(target)
            hold_for = None
            if link is None:
                holder = self.cfg.holder
                if item["state"] == "held" or item["kind"] == "hold" or not holder or holder == target:
                    return self._wait(item, f"{self.agent.devices.name_of(target)} is not reachable")
                link = await self.probe(holder)
                if link is None:
                    return self._wait(item, f"{self.agent.devices.name_of(target)} is not reachable")
                hold_for = target
            await self.transfer(item, link, hold_for, self.cfg.holder if hold_for else target)
        except Cancelled:
            self._live(item, item.get("_remote", target), item.get("_sent", 0), "cancelled")
        except Refused as e:
            self._fail(item, str(e))
        except NoSpace as e:
            self._wait(item, str(e))
        except (SendError, httpx.HTTPError, OSError) as e:
            self._wait(item, f"stopped: {e}" if str(e) else "stopped, will try again")
            if "_remote" in item:  # it was moving: the notification says it stopped (it tries again later)
                self._live(item, item["_remote"], item.get("_sent", 0), "failed")

    def _live(self, item: dict, remote: str, done: int, state: str = "moving") -> None:
        """Tell the agent how far this share has got (for Argus and the notification)."""
        manifest = item["manifest"]
        self.agent.progress(item["id"], "hold" if item["kind"] == "hold" else "send", src=self.cfg.device_id,
                            dst=remote, label=label_of(manifest), files=len(manifest), total=item["total"],
                            done=done, state=state)

    def _wait(self, item: dict, reason: str) -> None:
        state = "held" if item["state"] == "held" else "queued"
        self.agent.store.set(item["id"], state=state, reason=reason, attempts=item["attempts"] + 1,
                             next_try=time.time() + self.cfg.retry_seconds)

    def _fail(self, item: dict, reason: str) -> None:
        self.agent.store.set(item["id"], state="failed", reason=reason)
        self.agent.store.event("failed", peer=self.agent.devices.name_of(item["target"]),
                               label=label_of(item["manifest"]), detail=reason, offer_id=item["id"])

    async def transfer(self, item: dict, link: Link, hold_for: str | None, remote: str) -> None:
        store, oid, target = self.agent.store, item["id"], item["target"]
        manifest = item["manifest"]
        chunk = self.cfg.chunk_bytes
        offer = {"offer_id": oid, "from": self.cfg.device_id, "chunk": chunk, "total": item["total"],
                 "files": [{"n": n, "rel_path": f["rel_path"], "size": f["size"], "mtime": f["mtime"],
                            "sha256": f["sha256"]} for n, f in enumerate(manifest)]}
        if hold_for:
            offer["hold_for"] = hold_for
        if item["origin"]:
            offer["origin"] = item["origin"]
        store.set(oid, state="sending", route=link.kind, reason=None)
        run = Run(item, remote, link)
        item["_remote"] = remote
        self._live(item, remote, 0)
        watcher = asyncio.create_task(self._watch(run))
        try:
            rep = await self._offer(run, offer)
            work = deque((n, i) for n, have in rep["have"].items()
                         for i in range(-(-manifest[n]["size"] // chunk)) if i not in have)
            for _round in range(4):
                await self._pump(run, work)
                store.set(oid, state="verifying", sent=item["total"])
                self._live(item, remote, item["total"], "verifying")
                r = await self.call(run.cur, "POST", f"/send/offers/{oid}/done", target=remote)
                if r.status_code == 404:  # a lost reply: the offer was finished already; offer again, it knows
                    rep = await self._offer(run, offer)
                    r = await self.call(run.cur, "POST", f"/send/offers/{oid}/done", target=remote)
                if r.status_code == 200:
                    break
                if r.status_code == 409:
                    bad = r.json().get("bad", {})
                    work = deque((int(n), i) for n, idx in bad.items() for i in idx)
                    store.set(oid, state="sending")
                    continue
                raise SendError(f"{remote} answered {r.status_code}")
            else:
                raise SendError("files kept failing verification")
        finally:
            watcher.cancel()
        label, peer = label_of(manifest), self.agent.devices.name_of(target)
        self._live(item, remote, item["total"], "done")
        if item["kind"] == "send":  # Helios on this device can open what it sent (the originals, where they were)
            try:
                self.agent.remember("sent", oid, label, peer, [f["abs"] for f in manifest])
            except OSError:
                log.warning("could not note what %s sent", oid, exc_info=True)
        if hold_for:
            store.set(oid, state="held", holder=remote, reason=None, next_try=time.time() + self.cfg.retry_seconds)
            store.event("held", peer=peer, label=label, nbytes=item["total"], offer_id=oid,
                        detail=f"waiting on {self.agent.devices.name_of(remote)}")
            return
        store.set(oid, state="done", reason=None, sent=item["total"])
        store.event("delivered" if item["kind"] == "hold" else "sent", peer=peer, label=label, nbytes=item["total"],
                    offer_id=oid)
        if item["kind"] == "hold":
            self.agent.drop_hold_files(oid)
            await self._tell_origin(item)
        elif item.get("holder"):  # reached the target directly first: tell the holder to drop its copy
            try:
                hl = await self.probe(item["holder"])
                if hl:
                    await self.call(hl, "DELETE", f"/send/holding/{oid}", target=item["holder"])
            except (Refused, httpx.HTTPError):
                pass

    async def _tell_origin(self, item: dict) -> None:
        """The held share reached its target: tell the original sender (the 'delivered' event also goes to Argus,
        which is how a sender that cannot be reached right now finds out)."""
        origin = item.get("origin")
        if not origin or origin == self.cfg.device_id:
            return
        try:
            link = await self.probe(origin)
            if link:
                await self.call(link, "POST", f"/send/offers/{item['id']}/delivered", target=origin)
        except (Refused, httpx.HTTPError):
            pass

    async def _offer(self, run: Run, offer: dict) -> dict:
        r = await self.call(run.cur, "POST", "/send/offers", body=json.dumps(offer).encode(), target=run.target)
        if r.status_code == 507:
            missing = r.json().get("missing_bytes", 0)
            raise NoSpace(f"not enough room on {self.agent.devices.name_of(run.target)} (needs {missing} more bytes)")
        if r.status_code == 403:
            raise Refused(r.json().get("detail", "refused"))
        if r.status_code != 200:
            raise SendError(f"offer answered {r.status_code}")
        rep = r.json()
        return {"have": {int(k): set(v) for k, v in rep["have"].items()}, "done": rep["done"]}

    async def _pump(self, run: Run, work: deque) -> None:
        if not work:
            return
        small = all(f["size"] < SMALL for f in run.item["manifest"])
        n_workers = 8 if small else self.cfg.parallel
        failure: list[BaseException] = []

        async def worker() -> None:
            while work and not failure:
                n, i = work.popleft()
                try:
                    await self._chunk(run, n, i)
                except BaseException as e:  # noqa: BLE001
                    failure.append(e)
                    return

        await asyncio.gather(*(worker() for _ in range(min(n_workers, len(work)))))
        if failure:
            raise failure[0]

    async def _chunk(self, run: Run, n: int, i: int) -> None:
        f = run.item["manifest"][n]
        size = self.cfg.chunk_bytes
        data = await asyncio.to_thread(read_range, f["abs"], i * size, min(size, f["size"] - i * size))
        sha = hashlib.sha256(data).hexdigest()
        err: Exception | None = None
        for attempt in range(ATTEMPTS):
            link = run.cur
            try:
                r = await self.call(link, "PUT", f"/send/offers/{run.item['id']}/files/{n}", query=f"chunk={i}",
                                    body=data, chunk_sha=sha, target=run.target)
                if r.status_code == 200:
                    run.sent += len(data)
                    run.item["_sent"] = run.sent
                    if time.time() - run.shown > 0.5:
                        run.shown = time.time()
                        cur = self.agent.store.get(run.item["id"])
                        if cur and cur["state"] == "cancelled":
                            raise Cancelled()
                        self.agent.store.set(run.item["id"], sent=run.sent)
                        self._live(run.item, run.target, run.sent)
                    return
                if r.status_code == 507:
                    raise NoSpace("the other device ran out of room")
                err = SendError(f"chunk answered {r.status_code}")  # 422: damaged on the way, send it again
            except httpx.HTTPError as e:
                err = e
                await self._failover(run, link)
            await asyncio.sleep(min(0.05 * (attempt + 1), 0.5))
        raise SendError(f"chunk {i} of {f['rel_path']} did not get through: {err}")

    async def _failover(self, run: Run, failed: Link) -> None:
        """The path in use broke: the other kind, if it answers."""
        if run.cur is not failed:
            return  # someone switched already
        other = "tailscale" if failed.kind == "lan" else "lan"
        try:
            alt = await self.probe(run.target, other)
        except Refused:
            alt = None
        if alt and run.cur is failed:
            run.cur = alt

    async def _watch(self, run: Run) -> None:
        """While on Tailscale, try the LAN every few seconds; the chunks still to go then move over."""
        while True:
            await asyncio.sleep(self.cfg.lan_probe_seconds)
            if run.cur.kind != "lan":
                try:
                    lan = await self.probe(run.target, "lan")
                except (Refused, httpx.HTTPError):
                    lan = None
                if lan:
                    run.cur = lan
                    self.agent.store.set(run.item["id"], route="lan")
