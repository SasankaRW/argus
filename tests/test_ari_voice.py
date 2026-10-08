"""Ari's one voice (the expressive voice server) and hearing (Whisper, on the PC's worker), with fakes for both."""

from __future__ import annotations

import sys
import threading
import types
import urllib.request
from http.server import ThreadingHTTPServer

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


def fake_voice():
    """A voice server that answers (the Chatterbox stand-in); returns it and its url."""
    import numpy as np

    from argus import voice_server as vs

    class Engine:
        kind, device, m = "turbo", "cpu", None

        def say(self, text, clip=None):
            return vs.to_wav(np.zeros(2400, np.float32), 24000)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), vs.handler(Engine()))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_ari_speaks_with_its_one_voice(tmp_path, monkeypatch):
    voice, url = fake_voice()
    a = with_cfg(tmp_path, monkeypatch, f"ari:\n  expressive_url: '{url}'\n")
    try:
        with Server(a.open()) as srv:
            cl = client(srv.url)
            assert cl.get("/ari-voice")["voice"] is True
            req = urllib.request.Request(f"{srv.url}/ari-voice/say", data=b'{"text": "Hello, I am Ari."}',
                                         headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req) as r:
                body = r.read()
                assert r.headers["Content-Type"] == "audio/wav" and body[:4] == b"RIFF"
    finally:
        voice.shutdown()


def test_a_voice_that_cant_speak_means_text_not_a_second_voice(tmp_path, monkeypatch):
    a = with_cfg(tmp_path, monkeypatch, "ari:\n  expressive_url: 'http://127.0.0.1:9'\n")  # nothing listens there
    with Server(a.open()) as srv:
        cl = client(srv.url)
        assert cl.get("/ari-voice")["hearing"] == "browser"
        with pytest.raises(ApiError) as e:
            cl.post("/ari-voice/say", {"text": "hi"})
        assert e.value.status == 409
        for gone in ("/ari-voice/voices", "/ari-voice/voice"):  # the voice picker went with Piper
            with pytest.raises(ApiError) as e:
                cl.get(gone)
            assert e.value.status in (404, 405)


def test_old_piper_settings_are_ignored(tmp_path):
    from argus.config import load_config

    (tmp_path / "argus.yaml").write_text("ari:\n  voice: data/voices/x.onnx\n  voice_engine: piper\n")
    assert load_config(tmp_path / "argus.yaml").ari.expressive_url.startswith("http://127.0.0.1")


def test_whisper_on_the_pc(tmp_path, monkeypatch):
    got = {}

    def fake(audio, name, prompt=""):
        got["audio"], got["model"], got["prompt"] = audio, name, prompt
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
    assert b"sort my downloads" in out["r"] and (got["audio"], got["model"]) == (b"OggS-audio", "base.en")
    assert "WhatsApp" in got["prompt"]  # Whisper is told the names to expect (argus.vocab)
    assert not list((tmp_path / "data" / "ari").glob("*")) if (tmp_path / "data" / "ari").exists() else True


def test_whisper_falls_back_to_the_cpu_once_and_for_all(monkeypatch):
    made = []

    class WhisperModel:
        def __init__(self, name, device, compute_type):
            made.append((name, device))
            self.device = device

        def transcribe(self, audio, **kw):
            if self.device == "cuda":
                raise RuntimeError("Library cublas64_12.dll is not found")
            return iter(()), None

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=WhisperModel))
    monkeypatch.setattr(hear, "_models", {})
    monkeypatch.setattr(hear, "_gpu", {"ok": True})
    hear.model("tiny.en")
    hear.model("small.en")  # the GPU is not tried again: a second try can hang
    assert made == [("tiny.en", "cuda"), ("tiny.en", "cpu"), ("small.en", "cpu")]


def test_a_gpu_check_that_hangs_counts_as_failed():
    class Stuck:
        def transcribe(self, audio, **kw):
            threading.Event().wait(5)
            return iter(()), None

    with pytest.raises(TimeoutError):
        hear._prove(Stuck(), limit=0.2)
