"""Clipboard and link sync: the wire vector, the watcher's decisions, two agents talking, the relay and the switches."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import sys
import time
from concurrent.futures import Future

import httpx
import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from _send_kit import Spy, make_agent, pair, wait_for
from argus.send import clip as clipmod
from argus.send.clip import ClipDisabled, ClipError, ClipSync, ClipWatcher, Flags, digest

SECRET = "correct horse battery staple 12345"


class FakeSource:
    """clip.ClipSource without Windows: tests set what is on the 'clipboard' and read what was written."""

    def __init__(self, text: str | None = None, flags: Flags | None = None):
        self._text, self._flags = text, flags or Flags()
        self.text_reads = 0
        self.writes: list[tuple[str, bool]] = []
        self.on_update = None
        self.started = 0
        self.stopped = 0

    def start(self, on_update):
        self.on_update, self.started = on_update, self.started + 1

    def stop(self):
        self.stopped += 1

    def flags(self):
        return self._flags

    def text(self):
        self.text_reads += 1
        return self._text

    def write(self, text, exclude=True):
        self.writes.append((text, exclude))


# ---------------------------------------------------------------------- the wire format

def test_shared_test_vector():
    key = clipmod.clip_key(bytes(range(32)))
    assert key.hex() == "b52c0827574da950beaf28d43afd56c5d15acd46885808c023a619251c9bf3a4"
    ct = AESGCM(key).encrypt(bytes(range(12)), b'{"text":"hello"}', None)
    assert base64.b64encode(ct).decode() == "OOYJvIsdC7IC6jK/7SwwxP9ypPKsANs3XdeBa0E92G4="


def test_seal_roundtrip_and_tamper():
    k = bytes(range(32))
    clip = {"id": "a", "kind": "copy", "text": "héllo ✓", "at": 1, "from": "pc", "for": "phone"}
    body = clipmod.seal(k, clip, nonce=bytes(range(12)))
    assert set(json.loads(body)) == {"n", "c"} and b"llo" not in body
    assert clipmod.unseal(k, body) == clip
    with pytest.raises(ValueError):
        clipmod.unseal(bytes(32), body)  # another session key
    d = json.loads(body)
    d["c"] = base64.b64encode(b"x" * 40).decode()
    with pytest.raises(ValueError):
        clipmod.unseal(k, json.dumps(d).encode())
    with pytest.raises(ValueError):
        clipmod.unseal(k, b"not json")


def test_url_helpers():
    assert clipmod.as_url(" https://example.com/a?b=1 ") == "https://example.com/a?b=1"
    for bad in ("example.com", "two words http://x.y", "javascript:alert(1)", "http://", "https://a.b\nhttps://c.d"):
        assert clipmod.as_url(bad) is None
    assert clipmod.short_url("https://www.example.com/a/b?token=zzz#f") == "www.example.com/a/b"
    assert len(clipmod.short_url("https://example.com/" + "x" * 200)) == 60


# ---------------------------------------------------------------------- the watcher's decisions (no Windows)

def watcher(src, sent, enabled=lambda: True):
    return ClipWatcher(src, sent.append, enabled, debounce=0.02)


def test_watcher_sends_text_once_and_dedupes_by_hash():
    src, sent = FakeSource("hello"), []
    w = watcher(src, sent)
    assert w.process() == "hello" and w.process() is None  # the same text twice in a row: once
    src._text = "other"
    assert w.process() == "other"
    src._text = "hello"
    assert w.process() == "hello"  # not "twice in a row" any more
    assert sent == ["hello", "other", "hello"]


def test_watcher_echo_guard_skips_what_we_wrote():
    src, sent = FakeSource("from the phone"), []
    w = watcher(src, sent)
    w.written = digest("from the phone")
    assert w.process() is None and sent == []
    src._text = "mine"
    assert w.process() == "mine"


@pytest.mark.parametrize("flags", [Flags(exclude=True), Flags(history=0), Flags(cloud=0)])
def test_watcher_skips_excluded_copies_without_reading_the_text(flags):
    src, sent = FakeSource(SECRET, flags), []
    assert watcher(src, sent).process() is None
    assert sent == [] and src.text_reads == 0  # a password manager's copy is never even read
    assert Flags(history=1, cloud=1).excluded is False


def test_watcher_skips_empty_huge_unreadable_and_disabled():
    sent = []
    for text in (None, "", "x" * (clipmod.MAX_BYTES + 1)):
        assert watcher(FakeSource(text), sent).process() is None
    assert watcher(FakeSource("é" * (clipmod.MAX_BYTES // 2 + 1)), sent).process() is None  # bytes, not characters
    src = FakeSource("hi")
    src.flags = lambda: None  # the clipboard stayed busy
    assert watcher(src, sent).process() is None
    assert watcher(FakeSource("hi"), sent, enabled=lambda: False).process() is None
    assert sent == []


def test_watcher_debounces_a_burst_to_the_last_copy():
    src, sent = FakeSource("a"), []
    w = ClipWatcher(src, sent.append, debounce=0.1)
    for t in ("a", "b", "c"):
        src._text = t
        w.update()
    time.sleep(0.4)
    assert sent == ["c"]
    w.forget_sent("c")  # a failed send: copying it again must go
    assert w.last_sent is None


# ---------------------------------------------------------------------- two agents

def two(tmp_path, **over):
    pc = make_agent(tmp_path, "pc", "PC", **over.get("pc", {}))
    phone = make_agent(tmp_path, "phone", "my phone", **over.get("phone", {}))
    pair(pc, phone)
    pc.src, phone.src = FakeSource(), FakeSource()
    pc.clip.attach(pc.src)
    phone.clip.attach(phone.src)
    phone.links = []
    phone.clip.on_link = lambda url, origin: phone.links.append((url, origin))
    return pc, phone


def test_copy_clip_roundtrips_through_the_receiver(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)

    async def go():
        pc, phone = two(tmp_path)
        await pc.start()
        await phone.start()
        try:
            assert await pc.clip.send("copy", SECRET) == "direct"
            await wait_for(lambda: phone.src.writes, what="the clip on the phone")
            assert phone.src.writes == [(SECRET, True)]  # marked so Windows clipboard history skips it
            assert phone.clip.watcher.written == digest(SECRET)  # and it will not be sent back
            assert phone.clip.watcher.process() is None  # the phone's clipboard now holds it: no echo
            phone.src._text = SECRET
            assert phone.clip.watcher.process() is None
            assert SECRET not in caplog.text
            assert f"clip sent to phone ({len(SECRET)} chars)" in caplog.text
            assert not [p for p in phone.data.rglob("*") if p.is_file() and SECRET in p.read_text(errors="ignore")]
        finally:
            await pc.stop()
            await phone.stop()

    asyncio.run(go())


def test_link_clip_becomes_a_notification_not_a_clipboard_write(tmp_path):
    async def go():
        pc, phone = two(tmp_path)
        await pc.start()
        await phone.start()
        try:
            await pc.clip.send("link", "https://example.com/page")
            await wait_for(lambda: phone.links, what="the link")
            assert phone.links == [("https://example.com/page", "pc")] and phone.src.writes == []
            await pc.clip.send("link", "javascript:alert(1)")  # accepted on the wire, never shown
            await asyncio.sleep(0.1)
            assert len(phone.links) == 1
        finally:
            await pc.stop()
            await phone.stop()

    asyncio.run(go())


def test_receiver_dedupes_ids_and_orders_per_sender(tmp_path):
    async def go():
        pc, phone = two(tmp_path)
        phone.cfg.holder = "calypso"
        base = 1_700_000_000_000

        def clip(i, at, sender="pc", text="t"):
            return {"id": i, "kind": "copy", "text": text, "at": at, "from": sender, "for": "phone"}

        c = phone.clip
        assert c.receive(clip("1", base), "pc") == {"ok": True, "apply": True}
        assert c.receive(clip("1", base + 5), "pc") == {"ok": True, "dup": True}  # same id again
        assert c.receive(clip("2", base - 60_000), "pc").get("apply")  # a little late: still fine
        assert c.receive(clip("3", base - 6 * 60_000), "pc") == {"ok": True, "stale": True}  # too old: dropped
        assert c.receive(clip("4", base - 6 * 60_000, "other"), "calypso").get("apply")  # clocks never meet
        with pytest.raises(ClipError):  # `from` is trusted only from the holder
            c.receive(clip("5", base, "calypso"), "pc")
        with pytest.raises(ClipError):
            c.receive({**clip("6", base), "kind": "paste"}, "pc")
        with pytest.raises(ClipError):
            c.receive({**clip("7", base), "text": ""}, "pc")
        with pytest.raises(ClipError):
            c.receive({**clip("8", base), "for": "somebody"}, "pc")
        for i in range(70):  # only the last 64 ids are kept
            c.receive(clip(f"x{i}", base + i), "pc")
        assert len(c.seen) == 64 and "1" not in c.seen

    asyncio.run(go())


def test_sync_off_refuses_with_409_both_ways(tmp_path):
    async def go():
        pc, phone = two(tmp_path)
        await pc.start()
        await phone.start()
        try:
            phone.clip.set_enabled(False)
            with pytest.raises(ClipDisabled):
                await pc.clip.send("copy", "nope")
            assert phone.src.writes == []
            pc.clip.set_enabled(False)
            with pytest.raises(ClipDisabled):  # and it does not even try when it is off here
                await pc.clip.send("copy", "nope")
            raw = await pc.sender.probe("phone")
            r = await pc.sender.call(raw, "POST", "/send/clip", body=clipmod.seal(raw.key, pc.clip.make("copy", "x")),
                                     target="phone")
            assert r.status_code == 409
            assert pc.src.stopped >= 1 and phone.src.stopped >= 1  # the listener stops while sync is off
            phone.clip.set_enabled(True)
            assert phone.src.started >= 2
        finally:
            await pc.stop()
            await phone.stop()

    asyncio.run(go())


# ---------------------------------------------------------------------- the holder

def three(tmp_path):
    spy_pc, spy_hold = Spy(), Spy()
    pc = make_agent(tmp_path, "pc", "PC", holder="calypso", transport=spy_pc)
    hold = make_agent(tmp_path, "calypso", "Calypso", holding=True, transport=spy_hold)
    phone = make_agent(tmp_path, "phone", "my phone", holder="calypso")
    for a, b in ((pc, hold), (pc, phone), (hold, phone)):
        pair(a, b)
    pc.src, phone.src = FakeSource(), FakeSource()
    pc.clip.attach(pc.src)
    phone.clip.attach(phone.src)
    return pc, hold, phone, spy_pc, spy_hold


def clip_bodies(spy):
    return [json.loads(b)["c"] for m, u, h, b in spy.sniffed if m == "POST" and u.endswith("/send/clip")]


def test_relay_holds_in_ram_reencrypts_and_expires(tmp_path):
    async def go():
        pc, hold, phone, spy_pc, spy_hold = three(tmp_path)
        now = [1000.0]
        hold.clip.clock = lambda: now[0]
        await pc.start()
        await hold.start()
        try:
            assert await pc.clip.send("copy", SECRET) == "holder"  # the phone is off
            assert hold.clip.pending["phone"].clip["text"] == SECRET
            assert hold.clip.pending["phone"].clip["from"] == "pc"  # the origin is kept
            await wait_for(lambda: not hold.clip._pushing, what="the first push attempt")
            assert not [p for p in hold.data.rglob("*") if p.is_file() and SECRET in p.read_text(errors="ignore")]
            # a newer clip replaces the pending one
            await pc.clip.send("copy", "newer")
            assert hold.clip.pending["phone"].clip["text"] == "newer" and len(hold.clip.pending) == 1
            await wait_for(lambda: not hold.clip._pushing, what="the push attempt")
            # the phone comes back: Calypso pushes it, re-encrypted with the phone's session key
            await phone.start()
            try:
                assert await hold.clip.push() == 1
                await wait_for(lambda: phone.src.writes, what="the delivery")
                assert phone.src.writes == [("newer", True)] and hold.clip.pending == {}
                to_holder, to_phone = clip_bodies(spy_pc)[-1], clip_bodies(spy_hold)[-1]
                assert to_holder != to_phone  # decrypted and sealed again, not forwarded
                assert await hold.clip.push() == 0
            finally:
                await phone.stop()
            # a clip nobody collects is dropped after 10 minutes
            await pc.clip.send("copy", "stale soon")
            await wait_for(lambda: not hold.clip._pushing, what="the push attempt")
            assert "phone" in hold.clip.pending
            now[0] += clipmod.PENDING_TTL + 1
            assert await hold.clip.push() == 0 and hold.clip.pending == {}
        finally:
            await pc.stop()
            await hold.stop()

    asyncio.run(go())


def test_a_clip_for_the_holder_itself_and_sync_off_on_the_holder(tmp_path):
    async def go():
        pc, hold, phone, _a, _b = three(tmp_path)
        pc.clip.peer = "calypso"  # nobody is an endpoint at Calypso
        await pc.start()
        await hold.start()
        try:
            with pytest.raises(ClipError):
                await pc.clip.send("copy", "x")
            pc.clip.peer = "phone"
            hold.clip.set_enabled(False)
            with pytest.raises(ClipError):  # holder off: refused (409), and the phone is off too
                await pc.clip.send("copy", "x")
            assert hold.clip.pending == {}
        finally:
            await pc.stop()
            await hold.stop()

    asyncio.run(go())


def test_unreachable_without_a_holder_raises_and_forgets_the_copy(tmp_path):
    async def go():
        pc, _phone = two(tmp_path)  # the phone is not started, there is no holder
        with pytest.raises(ClipError):
            await pc.clip.send("copy", "x")
        pc.clip.watcher.last_sent = digest("x")
        pc.clip._done(_failed(), "x")
        assert pc.clip.watcher.last_sent is None

    asyncio.run(go())


def _failed():
    f = Future()
    f.set_exception(ClipError("nobody"))
    return f


# ---------------------------------------------------------------------- the local, token-only routes

def test_local_routes_need_the_worker_token_and_persist_only_the_switch(tmp_path):
    async def go():
        pc, phone = two(tmp_path)
        pc.argus_token = "t0ken"
        await pc.start()
        await phone.start()
        base = f"http://127.0.0.1:{pc.cfg.port}"
        ok = {"Authorization": "Bearer t0ken"}
        try:
            async with httpx.AsyncClient() as c:
                for method, path, body in (("GET", "/send/clip", None),
                                           ("POST", "/send/clip/enabled", {"enabled": False}),
                                           ("POST", "/send/clip/link", {"url": "https://example.com"})):
                    assert (await c.request(method, base + path, json=body)).status_code == 401
                    assert (await c.request(method, base + path, json=body,
                                            headers={"Authorization": "Bearer nope"})).status_code == 401
                st = (await c.get(base + "/send/clip", headers=ok)).json()
                assert st["enabled"] is True and st["peer"] == "phone" and st["peer_name"] == "my phone"
                r = await c.post(base + "/send/clip/link", json={"url": "not a link"}, headers=ok)
                assert r.status_code == 400
                r = await c.post(base + "/send/clip/link", json={"url": "https://example.com/x"}, headers=ok)
                assert r.status_code == 200 and r.json() == {"ok": True, "via": "direct"}
                await wait_for(lambda: phone.src is not None and phone.clip.seen, what="the link on the phone")
                r = await c.post(base + "/send/clip/enabled", json={"enabled": False}, headers=ok)
                assert r.json()["enabled"] is False
                assert json.loads((pc.data / "clip.json").read_text()) == {"enabled": False}  # nothing else
                assert (await c.post(base + "/send/clip/link", json={"url": "https://example.com/x"},
                                     headers=ok)).status_code == 409
            assert ClipSync(pc).enabled is False  # the switch survives a restart
            await phone.stop()
            pc.clip.set_enabled(True)
            async with httpx.AsyncClient() as c:
                r = await c.post(base + "/send/clip/link", json={"url": "https://example.com/x"}, headers=ok)
                assert r.status_code == 502  # nobody to take it
        finally:
            await pc.stop()

    asyncio.run(go())


def test_peer_defaults(tmp_path):
    pc = make_agent(tmp_path, "pc", "PC")
    phone = make_agent(tmp_path, "phone", "my phone")
    assert pc.clip.peer == "phone" and phone.clip.peer == "pc"
    assert make_agent(tmp_path, "laptop", "L", clip={"peer": "pc", "enabled": False}).clip.enabled is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_windows_listener_starts_and_stops_without_touching_the_clipboard():
    from argus.send.clipwin import WinClipboard

    w = WinClipboard()
    w.start(lambda: None)
    try:
        assert w._thread is not None and w._thread.is_alive() and w._hwnd
    finally:
        w.stop()
    assert w._thread is None


# ---------------------------------------------------------------------- the island and the command line

def test_island_offers_a_link_only_for_one_web_link_and_reads_the_agent_state():
    from argus.ari.ari_popup import Api, clipboard_url, fetch_info

    assert clipboard_url("https://example.com/x") == "https://example.com/x"
    for bad in ("", "hello https://example.com", "ftp://x.y", "https://a.b https://c.d", "just text"):
        assert clipboard_url(bad) is None

    class Local(Api):
        def call(self, method, path, body=None, timeout=6):
            if path == "/send/clip":
                return {"enabled": True, "peer": "phone"}
            raise OSError("no argus")

    assert fetch_info(Api("http://127.0.0.1:1", None), Local("http://127.0.0.1:2", None)).clip["enabled"] is True
    assert fetch_info(Api("http://127.0.0.1:1", None)).clip is None


def test_cli_send_link_calls_the_local_agent(tmp_path, monkeypatch, capsys):
    from argus.send import cli

    calls = []

    def fake(cfg, method, path, body=None, opener=None):
        calls.append((method, path, body))
        return 200, {"ok": True, "via": "holder"}

    cfg = tmp_path / "argus.yaml"
    cfg.write_text("logging:\n  file: null\nsend:\n  enabled: true\n  device_id: pc\n", encoding="utf-8")
    monkeypatch.setattr(cli, "local_call", fake)
    assert cli.main(["--config", str(cfg), "send", "--link", "https://example.com/a"]) == 0
    assert calls == [("POST", "/send/clip/link", {"url": "https://example.com/a"})]
    assert "queued" in capsys.readouterr().out
    problems = []
    monkeypatch.setattr(cli, "_problem", problems.append)
    assert cli.main(["--config", str(cfg), "send", "--link", "not a link"]) == 2 and problems
    with pytest.raises(SystemExit):
        cli.main(["--config", str(cfg), "send"])  # neither a device with paths nor a link
