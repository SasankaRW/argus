"""Hey Ari on the PC's microphone: cutting speech out of the stream, the wake phrase, follow-ups (no real audio)."""

from __future__ import annotations

import numpy as np
import pytest

from argus.ari_listen import BLOCK, RATE, Listener, Segmenter, wake_rest


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
