"""Ari's natural voice (Piper, in argusd) and hearing (Whisper, on the PC's worker), with fakes for both."""

from __future__ import annotations

import sys
import threading
import types
import urllib.request
import wave

import pytest

import argus.worker.hear as hear
from argus.worker import Worker
from argus.worker.client import ApiError
from test_ask import make
from test_worker import Server, client, wait_for


def with_cfg(tmp_path, monkeypatch, extra: str):
    a = make(tmp_path, monkeypatch)
    y = (tmp_path / "argus.yaml").read_text() + extra
    (tmp_path / "argus.yaml").write_text(y)
    from argus.config import load_config
    from argus.context import Argus
    return Argus(load_config(tmp_path / "argus.yaml")) if a else None


def fake_piper(monkeypatch):
    class PiperVoice:
        @staticmethod
        def load(path):
            return PiperVoice()

        def synthesize_wav(self, text, wf: wave.Wave_write):
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(22050)
            wf.writeframes(b"\x00\x00" * 2205)

    monkeypatch.setitem(sys.modules, "piper", types.SimpleNamespace(PiperVoice=PiperVoice))


def test_natural_voice(tmp_path, monkeypatch):
    fake_piper(monkeypatch)
    (tmp_path / "v.onnx").write_bytes(b"x")
    a = with_cfg(tmp_path, monkeypatch, f"ari:\n  voice: '{(tmp_path / 'v.onnx').as_posix()}'\n")
    with Server(a.open()) as srv:
        cl = client(srv.url)
        assert cl.get("/ari-voice")["voice"] is True
        req = urllib.request.Request(f"{srv.url}/ari-voice/say", data=b'{"text": "Hello, I am Ari."}',
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req) as r:
            body = r.read()
            assert r.headers["Content-Type"] == "audio/wav" and body[:4] == b"RIFF"


def test_no_voice_set_means_the_browser(tmp_path, monkeypatch):
    with Server(make(tmp_path, monkeypatch).open()) as srv:
        cl = client(srv.url)
        assert cl.get("/ari-voice") == {"voice": False, "hearing": "browser", "whisper_ready": False,
                                           "popup_here": False, "pill": "pulse"}
        with pytest.raises(ApiError) as e:
            cl.post("/ari-voice/say", {"text": "hi"})
        assert e.value.status == 409


def test_whisper_on_the_pc(tmp_path, monkeypatch):
    got = {}

    def fake(audio, name):
        got["audio"], got["model"] = audio, name
        return "sort my downloads"

    monkeypatch.setattr(hear, "transcribe", fake)
    a = with_cfg(tmp_path, monkeypatch, "ari:\n  hearing: whisper\n  whisper_model: base.en\n")
    with Server(a.open()) as srv:
        cl = client(srv.url)
        req = urllib.request.Request(f"{srv.url}/ari-voice/hear", data=b"OggS-audio", method="POST",
                                     headers={"Content-Type": "audio/ogg"})
        with pytest.raises(urllib.error.HTTPError) as e:  # no PC worker yet
            urllib.request.urlopen(req)
        assert e.value.code == 409
        w = Worker(cl, "pc", capabilities=["desktop", "gpu"], watch_folders=False)
        w.register()
        assert cl.get("/ari-voice")["whisper_ready"] is True
        out = {}
        t = threading.Thread(target=lambda: out.update(r=urllib.request.urlopen(req).read()))
        t.start()
        wait_for(lambda: w.run_once(wait=1))
        t.join(10)
    assert b"sort my downloads" in out["r"] and got == {"audio": b"OggS-audio", "model": "base.en"}
    assert not list((tmp_path / "data" / "ari").glob("*")) if (tmp_path / "data" / "ari").exists() else True


def test_pick_a_voice_and_speed(tmp_path, monkeypatch):
    fake_piper(monkeypatch)
    voices = tmp_path / "voices"
    voices.mkdir()
    for n in ("en_US-lessac-medium", "en_GB-alan-medium"):
        (voices / f"{n}.onnx").write_bytes(b"x")
        (voices / f"{n}.onnx.json").write_text("{}")
    cfg = f"ari:\n  voice: '{(voices / 'en_US-lessac-medium.onnx').as_posix()}'\n"
    a = with_cfg(tmp_path, monkeypatch, cfg)
    with Server(a.open()) as srv:
        cl = client(srv.url)
        v = cl.get("/ari-voice/voices")
        assert v["current"] == "en_US-lessac-medium" and v["speed"] == 1.0
        have = {x["id"]: x["installed"] for x in v["voices"]}
        assert have["en_GB-alan-medium"] is True and have["en_US-amy-medium"] is False
        v = cl.call("PUT", "/ari-voice/voice", {"voice": "en_GB-alan-medium", "speed": 1.2})[1]
        assert v["current"] == "en_GB-alan-medium" and v["speed"] == 1.2
        with pytest.raises(ApiError) as e:  # not a voice name: never downloaded
            cl.call("PUT", "/ari-voice/voice", {"voice": "../evil", "speed": 1.0})
        assert e.value.status in (409, 422)
    # The pick survives a restart of argusd.
    from argus.config import load_config
    from argus.context import Argus
    with Server(Argus(load_config(tmp_path / "argus.yaml")).open()) as srv:
        v = client(srv.url).get("/ari-voice/voices")
        assert v["current"] == "en_GB-alan-medium" and v["speed"] == 1.2
