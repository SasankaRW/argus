"""Ari P2: fewer false starts, the PC's own sound ignored, a wake-word model, a quiet mic noticed."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from argus import ari_listen as al
from argus import health
from argus import voice_live as vl
from argus.config import Config


@pytest.mark.parametrize("said,rest", [
    ("Hey Ari, play some music", "play some music"),
    ("um, hey Ari what's the time", "what's the time"),
    ("Ari, open Spotify", "open Spotify"),
    ("OK Harry, what's running", "what's running"),           # a mishearing of "Ari", but with "OK" first
    ("so I told Ari about it", None),                        # the name in the middle: not for Ari
    ("the Argus team won", None),
    ("a lorry went past", None),
    ("Harry, come here", None),                              # a bare mishearing doesn't count
])
def test_the_wake_phrase_only_counts_at_the_start(said, rest):
    assert al.wake_rest(said, strict=False) == rest


def test_while_the_pc_plays_sound_only_a_clear_hey_ari_counts():
    assert al.wake_rest("Ari, open Spotify", strict=True) is None
    assert al.wake_rest("OK Harry, what's running", strict=True) is None
    assert al.wake_rest("Hey Ari, open Spotify", strict=True) == "open Spotify"


def test_whispers_made_up_words_are_dropped():
    seg = lambda text, nsp, lp: SimpleNamespace(text=text, no_speech_prob=nsp, avg_logprob=lp)  # noqa: E731
    assert al.made_up(seg(" Thank you for watching.", 0.5, -0.4))
    assert al.made_up(seg(" something", 0.8, -1.2))
    assert not al.made_up(seg(" Thank you.", 0.05, -0.2))  # really said, clearly
    assert not al.made_up(seg(" Hey Ari, open Spotify", 0.1, -0.3))


def test_the_pcs_own_sound_is_ignored_but_you_get_through():
    t = [0.0]
    g = vl.PlaybackGuard(clock=lambda: t[0])
    assert not g.active and not g.explains(0.05)  # nothing playing: everything is you
    for _ in range(60):  # a video plays; a fifth of it comes back through the mic
        t[0] += 0.032
        g.played(0.1)
        assert g.explains(0.02) or len(g._ratios) < 20
    assert g.active and 0.15 < g.coupling() < 0.25
    assert g.explains(0.03)  # still just the video
    assert not g.explains(0.2)  # you, talking over it
    t[0] += 3.0
    g.played(0.0)
    assert not g.active  # the video stopped


def test_the_wake_word_model_wakes_ari_once():
    woke: list[int] = []

    class Model:
        def __init__(self):
            self.scores = [0.1, 0.2, 0.9, 0.95, 0.1]

        def predict(self, chunk):
            assert chunk.dtype == np.int16 and len(chunk) == al.WakeWord.CHUNK
            return {"hey_ari": self.scores.pop(0) if self.scores else 0.0}

    t = [0.0]
    w = al.WakeWord(Model(), 0.5, lambda: woke.append(1), clock=lambda: t[0])
    for _ in range(12):  # 12 frames of 512 = 6144 samples = 4.8 chunks
        t[0] += 0.032
        w.feed(np.zeros(vl.FRAME, dtype=np.float32))
    assert woke == [1]  # 0.9 then 0.95: one wake (the cool-down)


def test_a_quiet_mic_is_noticed():
    cfg = Config.model_validate({"ari": {"listen": True}})
    r = health.listener(cfg, 100.0, 120.0, {"mic": "Webcam mic", "loudest": 0.003})
    assert r["level"] == "warn" and "Webcam mic" in r["detail"]
    ok = health.listener(cfg, 100.0, 120.0, {"mic": "Headset", "loudest": 0.05})
    assert ok["level"] == "ok" and "Headset" in ok["detail"]


def test_the_listener_reports_its_mic(tmp_path):
    from test_ari_think import make
    from test_worker import Server, client

    with Server(make(tmp_path).open()) as srv:
        cl = client(srv.url)
        cl.post("/ari/listener", {"mic": "Headset", "loudest": 0.004})
        checks = cl.post("/ari/health", {})["health"]["checks"]
        # listen is off in this config, so no listener row; the info is still kept for when it is on
        assert all(c["name"] != "listener" for c in checks)
