"""Hey Ari on the PC's microphone: the wake phrase, what counts after an answer, Whisper reading its hints back,
speaking up (no real audio)."""

from __future__ import annotations

import pytest

from argus import ari_listen, vocab
from argus.ari_listen import follow_ok, notice_text, speak_hours_ok, wake_rest


@pytest.mark.parametrize("said,rest", [
    ("Hey Ari, sort my downloads.", "sort my downloads"),
    ("Hey Harry what's running", "what's running"),
    ("OK Ari.", ""),
    ("Ari, shut down the PC", "shut down the PC"),
    ("I was talking to Harry yesterday", None),
    ("How are you?", None),
])
def test_wake_phrase(said, rest):
    assert wake_rest(said) == rest


def test_whisper_reading_the_names_back_is_not_a_question():
    names = ["Kaancha", "Do Not", "Disturb", "PC", "Ari", "Argus", "Helios", "WhatsApp", "Snapchat", "Zoom"]
    for noise in ("about Do Not, Disturb, PC, Bluetooth", "Argus, Helios, WhatsApp, Snapchat, Snapchat",
                  "Talking to Ari about Do Not, Disturb, PC, Zoom, Discord.", "about Do Not, Disturb"):
        assert vocab.echo(noise, names + vocab.BUILTIN), noise
    for real in ("Open WhatsApp", "call Kaancha on WhatsApp", "what's on my screen", "turn on Do Not Disturb",
                 "how's the weather tomorrow"):
        assert not vocab.echo(real, names + vocab.BUILTIN), real


def test_the_wake_check_gets_no_hints_and_the_request_gets_the_names(monkeypatch):
    calls: list = []

    class Seg:
        def __init__(self, text):
            self.text, self.no_speech_prob, self.avg_logprob = text, 0.0, -0.1

    class Model:
        def __init__(self, out):
            self.out = out

        def transcribe(self, audio, **kw):
            calls.append(kw)
            return iter([Seg(self.out)]), None

    out = {"text": "Hey Ari"}
    import argus.worker.hear as hear

    monkeypatch.setattr(hear, "model", lambda name: Model(out["text"]))
    monkeypatch.setitem(ari_listen.VOCAB, "words", ["Kaancha", "WhatsApp"])
    assert ari_listen.whisper("tiny.en", names=False)(b"") == "Hey Ari"
    assert calls[-1]["hotwords"] is None and "initial_prompt" not in calls[-1]
    out["text"] = "about Kaancha, WhatsApp, Spotify, Chrome"  # noise read back as the hints
    assert ari_listen.whisper("small.en")(b"") == ""
    assert calls[-1]["hotwords"] == "Kaancha, WhatsApp"


def test_after_an_answer_the_pcs_sound_and_long_talk_need_hey_ari(monkeypatch):
    monkeypatch.setitem(ari_listen.STRICT, "on", False)
    assert follow_ok("and tomorrow?")
    assert not follow_ok(" ".join(["word"] * (ari_listen.FOLLOW_MAX_WORDS + 1)))  # the TV, someone else
    monkeypatch.setitem(ari_listen.STRICT, "on", True)  # a video is playing
    assert not follow_ok("and tomorrow?")


def test_speaking_up_is_off_unless_you_turn_it_on():
    from argus.config import AriConfig

    assert AriConfig().speak_up is False
    assert AriConfig(live=False, follow_up=True).talk_idle_s == 10  # old settings still load (ignored)


def test_speaking_up_hours_and_words():
    import time as _t
    at = lambda h, m: _t.struct_time((2026, 10, 1, h, m, 0, 3, 274, 0))  # noqa: E731
    assert speak_hours_ok("08:00-22:00", at(9, 0)) and not speak_hours_ok("08:00-22:00", at(23, 30))
    assert speak_hours_ok("22:00-07:00", at(23, 0)) and not speak_hours_ok("22:00-07:00", at(12, 0))
    assert notice_text("Watcher: RTX 5080", "RTX 5080 is LKR 279,000, under your LKR 280,000.") == \
        "Heads up: Watcher: RTX 5080. RTX 5080 is LKR 279,000, under your LKR 280,000."
    assert notice_text("Argus backup failed", "disk full") == "Heads up: Argus backup failed. disk full"


def test_important_phone_messages_become_notices(tmp_path):
    import json

    from argus.config import load_config
    from argus.context import Argus
    from argus.outbox import add_message, phone_message
    from conftest import run as arun

    (tmp_path / "argus.yaml").write_text("logging:\n  file: null\n", encoding="utf-8")
    a = Argus(load_config(tmp_path / "argus.yaml")).open()

    def fn(c):
        add_message(c, 1.0, "phone", phone_message("Evening", "fine", priority="low"))
        add_message(c, 2.0, "phone", phone_message("Argus backup failed", "disk full", priority="high"))
        return [json.loads(r[0]) for r in c.execute("SELECT data FROM events WHERE kind = 'ari.notice'")]

    assert arun(a.store.write(fn)) == [{"title": "Argus backup failed", "text": "disk full"}]
    a.store.close()


def test_accepted_share_adds_the_accepted_languages():
    from argus.ari_listen import accepted_share

    probs = {"si": 0.7, "en": 0.2, "ta": 0.1}
    assert accepted_share(probs, ["en"]) == 0.2
    assert abs(accepted_share(probs, ["en", "si"]) - 0.9) < 1e-9
    assert accepted_share({}, ["en"]) == 0.0


def test_the_trained_model_is_used_once_argus_answers_later():
    from argus.ari_listen import CommandModel

    made = []
    cmd = CommandModel("small.en", lambda a: "small", lambda name: made.append(name) or (lambda a: name),
                       start=lambda f: f())
    assert cmd(b"x") == "small"
    cmd.use("small.en")
    assert made == []  # the one in use: nothing to load
    cmd.use("data/models/whisper-mine")  # Helios says: the model trained on your voice
    assert cmd(b"x") == "data/models/whisper-mine" and made == ["data/models/whisper-mine"]

    def broken(name):
        raise OSError("no such model")

    cmd2 = CommandModel("small.en", lambda a: "small", broken, start=lambda f: f())
    cmd2.use("nope")
    assert cmd2(b"x") == "small" and cmd2.loading == ""  # keeps the one it has
