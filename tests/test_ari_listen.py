"""Hey Ari on the PC's microphone: cutting speech out of the stream, the wake phrase, follow-ups (no real audio)."""

from __future__ import annotations

import numpy as np
import pytest

from argus.ari_listen import BLOCK, RATE, Listener, Segmenter, barge, notice_text, speak_hours_ok, wake_rest


def blocks(ms: int, level: float) -> list:
    n = ms * RATE // 1000 // BLOCK
    rng = np.random.default_rng(1)
    return [(rng.standard_normal(BLOCK) * level).astype(np.float32) for _ in range(n)]


def run(seg: Segmenter, stream: list) -> list:
    return [c for b in stream if (c := seg.feed(b)) is not None]


def test_speech_becomes_clips_and_the_room_does_not():
    seg = Segmenter()
    clips = run(seg, blocks(2000, 0.003) + blocks(900, 0.2) + blocks(1000, 0.003)
                + blocks(100, 0.2) + blocks(1000, 0.003)       # a click: too short
                + blocks(1500, 0.2) + blocks(1000, 0.003))
    assert len(clips) == 2
    assert 900 <= len(clips[0]) * BLOCK * 1000 // RATE <= 1700


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


def make(texts: list[str], answers: list[dict] | None = None):
    t = [0.0]
    sent, spoken, chimes = [], [], []
    it = iter(texts)
    ans = iter(answers or [{"reply": "ok"}] * 10)
    lis = Listener(transcribe_wake=lambda a: next(it), transcribe=lambda a: next(it),
                   say=lambda text: (sent.append(text), next(ans))[1], speak=spoken.append,
                   chime=lambda: chimes.append(1), clock=lambda: t[0])
    return lis, t, sent, spoken, chimes


def clip(ms=1000):
    return blocks(ms, 0.2)


def test_wake_and_command_in_one_breath():
    lis, t, sent, spoken, _ = make(["Hey Ari, what's running?"], [{"reply": "Nothing is running."}])
    lis.clip(clip())
    assert sent == ["what's running"] and spoken == ["Nothing is running."]


def test_wake_alone_then_the_command_then_a_yes():
    lis, t, sent, spoken, chimes = make(
        ["Hey Ari.", "sort downloads every morning at 7", "yes"],
        [{"reply": "Every day at 7 am, I'll sort Downloads. Shall I set that up?", "pending": {"kind": "schedule"}},
         {"reply": "Done."}])
    lis.clip(clip())
    assert chimes == [1] and sent == []
    t[0] = 3.0
    lis.clip(clip(2500))
    t[0] = 9.0  # answered within 8 s of the question: no wake phrase needed
    lis.clip(clip(500))
    assert sent == ["sort downloads every morning at 7", "yes"] and spoken[-1] == "Done."


def test_other_talk_is_ignored_and_long_talk_is_not_transcribed():
    lis, t, sent, spoken, chimes = make(["so then he said"])
    assert lis.clip(clip()) is None and sent == [] and chimes == []
    assert lis.clip(clip(6000)) is None  # longer than a wake phrase: not even written down


def test_follow_up_without_the_wake_phrase():
    lis, t, sent, spoken, _ = make(["Hey Ari, weather in Kandy?", "and tomorrow?", "and next week?"],
                                   [{"reply": "Showers."}, {"reply": "Sunny."}, {"reply": "x"}])
    lis.speak = lambda text: (spoken.append(text), 2.0)[1]  # takes 2 s to say
    lis.clip(clip())
    t[0] = 7.5  # within 2 s of speaking + 6 s
    lis.clip(clip())
    assert sent == ["weather in Kandy", "and tomorrow?"]
    t[0] = 30.0  # long after: needs "Hey Ari" again ("and next week?" has none)
    lis.clip(clip())
    assert sent == ["weather in Kandy", "and tomorrow?"]


def test_stop_or_a_new_question_while_ari_talks():
    assert barge("Stop.", "It's 29 degrees in Kandy") == ("stop", "")
    assert barge("okay wait", "...") == ("stop", "")
    assert barge("Hey Ari, what time is it", "It's 29 degrees") == ("wake", "what time is it")
    assert barge("29 degrees in Kandy", "It's 29 degrees in Kandy today") is None  # its own voice
    assert barge("the kettle is boiling", "...") is None
    lis, t, sent, spoken, chimes = make(["Hey Ari, what time is it?"], [{"reply": "It's 9."}])
    stopped = []
    lis.stop_speaking = lambda: stopped.append(1)
    assert lis.interrupt(clip(), "It's 29 degrees in Kandy") == "what time is it"
    assert stopped == [1] and sent == ["what time is it"]


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
