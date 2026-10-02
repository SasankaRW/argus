"""Ari's spoken style: moods and sounds in replies, the expressive voice server (Chatterbox stand-in), the fallback to
Piper, fillers while Ari works."""

from __future__ import annotations

import io
import json
import threading
import urllib.request
import wave
from http.server import ThreadingHTTPServer

import numpy as np

from argus import voice_live as vl
from argus import voice_server as vs
from argus.expressive import for_voice, mood_of, plain, tidy
from argus.voice import Expressive


def test_moods_and_sounds():
    r = tidy("Haha, you finally fixed it! [laugh] What was it? [laugh] [laugh] [sarcastic]", "playful")
    assert r == "[playful] Haha, you finally fixed it! [laugh] What was it? [laugh]"  # at most two sounds
    assert plain(r) == "Haha, you finally fixed it! What was it?"
    assert for_voice("[calm] Okay. [sighs] Long day.") == ("calm", "Okay. [sigh] Long day.")
    assert tidy("Sure.", "neutral") == "Sure." and tidy("Sure.", "grumpy") == "Sure."
    assert mood_of("[EXCITED] Yes!") == ("excited", "Yes!")


class FakeEngine:
    kind, device, m = "turbo", "cpu", None

    def __init__(self):
        self.said = []

    def say(self, text, clip=None):
        self.said.append((text, clip))
        if not plain(text):
            raise ValueError("nothing to say")
        return vs.to_wav(np.zeros(2400, np.float32), 24000)


def test_the_voice_server_and_argus_falling_back_to_piper(tmp_path):
    eng = FakeEngine()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), vs.handler(eng))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        assert json.loads(urllib.request.urlopen(url + "/health").read())["ok"] is True

        class Piper:
            calls = 0

            def say(self, text):
                Piper.calls += 1
                return vs.to_wav(np.zeros(100, np.float32), 22050)

        clip = tmp_path / "voices" / "ari-clip.wav"
        e = Expressive(url, clip, Piper())
        wav = e.say("[cheerful] Oh nice! [laugh]")
        with wave.open(io.BytesIO(wav)) as w:
            assert w.getframerate() == 24000
        assert eng.said[0] == ("[cheerful] Oh nice! [laugh]", str(clip.resolve()))
        assert clip.exists() and Piper.calls == 1  # the voice to sound like: made once from the Piper voice
        e.say("Again.")
        assert Piper.calls == 1
    finally:
        srv.shutdown()
    assert Expressive(url, None, Piper()).say("Hello") is None  # server gone: None, argusd uses Piper


def test_mood_to_chatterbox_settings():
    class M:
        sr = 24000
        device = "cpu"

        def __init__(self):
            self.kw = None

        def generate(self, text, temperature=0.8, exaggeration=0.5, cfg_weight=0.5):
            self.kw = {"text": text, "temperature": temperature, "exaggeration": exaggeration, "cfg_weight": cfg_weight}
            return np.zeros((1, 2400), np.float32)

    import pytest

    pytest.importorskip("torch")
    e = vs.Engine("standard")
    e.m = M()
    e.say("[excited] We won! [gasp]")
    assert e.m.kw == {"text": "We won! [gasp]", "temperature": 0.9, "exaggeration": 0.8, "cfg_weight": 0.35}


def test_fillers_play_first_and_count_as_talking():
    class Out:
        def __call__(self, rate, cb):
            self.cb = cb
            return self

    out = Out()
    p = vl.Player(lambda t: None, out, rate=8000)
    p.filler(np.full(800, 0.3, np.float32), 8000)
    assert p.busy
    buf = np.zeros((800, 1), np.float32)
    out.cb(buf, 800)
    assert buf.max() > 0.25  # the filler
    f = vl.Fillers(lambda t: (np.zeros(10, np.float32), 8000) if "sec" in t else None, ["One sec.", "Nope"])
    for _ in range(100):
        if f.ready:
            break
        threading.Event().wait(0.01)
    assert f.pick() is not None and len(f.ready) == 1


def test_a_live_reply_keeps_its_mood_on_every_sentence():
    said = []

    class P:
        busy, text = False, ""

        def say(self, parts):
            said.extend(parts)

        def pause(self):
            pass

        def resume(self):
            pass

        def stop(self):
            pass

    talk = vl.Talk(turns=vl.Turns(), transcribe=lambda a: "Hey Ari, how are you", wake_rest=lambda t: "how are you",
                   ask=lambda q: {"reply": "[cheerful] Pretty good, honestly! I sorted your downloads earlier."},
                   player=P())
    talk._turn(np.zeros(10, np.float32))
    assert said == ["[cheerful] Pretty good, honestly! I sorted your downloads earlier."] or \
        all(s.startswith("[cheerful] ") for s in said)
    said.clear()
    two = "[excited] Your build passed, first try! [serious] But the backup failed last night."
    talk.ask = lambda q: {"reply": two}
    talk._turn(np.zeros(10, np.float32))
    assert said == ["[excited] Your build passed, first try!", "[serious] But the backup failed last night."]


def test_a_reply_changes_mood_mid_way():
    from argus.expressive import phrases, plain

    r = "[excited] We won! [laugh] Ten nil. [sympathetic] Shame about the rain though."
    assert phrases(r) == [("excited", "We won! [laugh] Ten nil."), ("sympathetic", "Shame about the rain though.")]
    assert phrases("Hi there.") == [("neutral", "Hi there.")]
    assert phrases("[calm] One. [calm] Two.") == [("calm", "One. Two.")]
    assert plain(r) == "We won! Ten nil. Shame about the rain though."


def test_a_streamed_reply_is_spoken_as_it_arrives_and_the_rest_follows():
    calls = []

    class P:
        busy, text = False, ""

        def say(self, parts):
            calls.append(("say", parts))

        def add(self, parts):
            calls.append(("add", parts))

        def pause(self):
            pass

        def resume(self):
            pass

        def stop(self):
            pass

    def ask(q, partial):
        partial("Oh, long day?", "calm")
        partial("Oh, long day? I get it.", "calm")
        return {"reply": "[calm] Oh, long day? I get it. [playful] Want me to put on something chill?"}

    talk = vl.Talk(turns=vl.Turns(), transcribe=lambda a: "Hey Ari, I'm tired", wake_rest=lambda t: "I'm tired",
                   ask=ask, player=P())
    talk._turn(np.zeros(10, np.float32))
    assert calls == [("add", ["[calm] Oh, long day?"]), ("add", ["[calm] I get it."]),
                     ("add", ["[playful] Want me to put on something chill?"])]
    assert vl.after_spoken("Something else.", "Oh, long day?") is None


def test_the_reply_so_far_from_a_json_answer_still_arriving():
    from argus.worker.think import finished_part, reply_so_far

    assert reply_so_far('{"mood": "calm", "reply": "Long day? I get it. You sh') == \
        ("calm", "Long day? I get it. You sh")
    assert reply_so_far('{"mood": "excited", "reply": "Wow \\"nice\\"! Ne') == ("excited", 'Wow "nice"! Ne')
    assert reply_so_far('{"mo') == ("", "")
    assert finished_part("Long day? I get it. You sh") == "Long day? I get it."
    assert finished_part("No end yet") == ""


def test_the_player_adds_sentences_without_cutting_off_what_it_is_saying():
    class Out:
        def __call__(self, rate, cb):
            self.cb = cb
            return self

    made = []
    p = vl.Player(lambda t: (made.append(t) or np.full(80, 0.2, np.float32), 8000), Out(), rate=8000)
    p.say(["One."])
    p.add(["Two.", "Three."])
    for _ in range(200):
        if not p._making:
            break
        threading.Event().wait(0.01)
    assert made == ["One.", "Two.", "Three."] and len(p._chunks) == 3 and p.text == "One. Two. Three."


def test_warming_up_the_voice_says_one_word_and_waits_for_the_server(tmp_path):
    eng = FakeEngine()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), vs.handler(eng))
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    class Piper:
        def say(self, text):
            return vs.to_wav(np.zeros(100, np.float32), 22050)

    try:
        e = Expressive(f"http://127.0.0.1:{srv.server_address[1]}", tmp_path / "clip.wav", Piper())
        assert e.warm(tries=2, wait=0.01) is True and eng.said[0][0] == "Hi."
    finally:
        srv.shutdown()
    assert Expressive("http://127.0.0.1:9", None, Piper()).warm(tries=2, wait=0.01) is False


def test_the_model_is_loaded_once_even_when_two_ask_at_the_same_time():
    import time

    loads = []

    class Slow(vs.Engine):
        def _load(self):
            if self.m is None:  # what the real one does
                loads.append(1)
                time.sleep(0.05)
                self.m = object()
            return self.m

    e = Slow("turbo")
    ts = [threading.Thread(target=e.load) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(loads) == 1
