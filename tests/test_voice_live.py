"""Talking with Ari as a conversation: turns, barge-in and recovery, ending, the echo gate, the sentence player.
No sound card or models: voice scores, transcripts and the clock are made up."""

from __future__ import annotations

import hashlib
import io
import zipfile

import numpy as np

from argus import voice_live as vl
from argus.ari_listen import wake_rest

F = np.zeros(vl.FRAME, dtype=np.float32)
DT = vl.FRAME / vl.RATE


def feed(t: vl.Turns, seconds: float, voice: float, ari: bool = False) -> list:
    out = []
    for _ in range(round(seconds / DT)):
        if (e := t.feed(F, voice, ari)) is not None:
            out.append(e[0])
    return out


def test_turn_ends_on_silence_without_the_model():
    t = vl.Turns()
    assert feed(t, 0.1, 0.9) == []  # a click
    assert feed(t, 0.5, 0.1) == []
    assert feed(t, 1.0, 0.9) == ["start"]
    assert feed(t, 0.5, 0.1) == []  # a short pause is not the end
    assert feed(t, 0.4, 0.1) == ["end"]


def test_smart_turn_waits_for_an_unfinished_sentence():
    said = []
    t = vl.Turns(finished=lambda audio: said.append(len(audio)) or (0.1 if len(said) == 1 else 0.9))
    feed(t, 1.0, 0.9)
    assert feed(t, 1.5, 0.1) == []  # "remind me to…": not finished, keeps listening
    assert len(said) == 1  # asked once per pause
    feed(t, 0.5, 0.9)
    assert feed(t, 0.3, 0.1) == ["end"]  # "…call mum.": finished at 0.2 s


def test_smart_turn_gives_up_after_a_long_pause():
    t = vl.Turns(finished=lambda audio: 0.0)
    feed(t, 1.0, 0.9)
    assert feed(t, 2.6, 0.1) == ["end"]


def test_barge_needs_a_clear_voice_while_ari_talks():
    t = vl.Turns()
    assert feed(t, 1.0, 0.6, ari=True) == []  # what sounds like voice, but no more than the echo
    assert feed(t, 0.35, 0.95, ari=True) == ["barge"]


class FakePlayer:
    def __init__(self):
        self.said, self.busy, self.text, self.log = [], False, "", []

    def say(self, parts):
        self.said.append(" ".join(parts))
        self.text, self.busy = " ".join(parts), True
        self.log.append("say")

    def pause(self):
        self.log.append("pause")

    def resume(self):
        self.log.append("resume")

    def stop(self):
        self.busy = False
        self.log.append("stop")


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


def make(heard=None, follow_ok=None):
    heard = heard if heard is not None else []
    asked = []
    clock = Clock()
    p = FakePlayer()
    talk = vl.Talk(turns=vl.Turns(), transcribe=lambda a: heard.pop(0), wake_rest=wake_rest,
                   ask=lambda q: asked.append(q) or {"reply": f"Answer to {q}. More detail here."},
                   player=p, clock=clock, **({"follow_ok": follow_ok} if follow_ok else {}))
    return talk, p, asked, heard, clock


def speak(talk, seconds=1.0, ari=False):
    events = []
    for _ in range(round(seconds / DT)):
        events.append(talk.frame(F, 0.95))
    for _ in range(round(1.0 / DT)):
        events.append(talk.frame(F, 0.05))
    return [e for e in events if e]


def test_a_conversation_without_saying_hey_ari_each_time():
    talk, p, asked, heard, clock = make(heard=["Hey Ari, what's running?", "and the backups?", "thanks Ari"])
    speak(talk)
    assert asked == ["what's running"]
    assert p.said[0].startswith("Answer to what's running.")
    p.busy = False
    talk.tick()
    clock.t += 5
    speak(talk)
    assert asked[-1] == "and the backups"  # no wake phrase needed
    p.busy = False
    speak(talk)
    assert p.said[-1] == "Okay." and not talk.in_conversation


def test_without_the_wake_phrase_nothing_happens():
    talk, p, asked, heard, clock = make(heard=["what's on TV tonight"])
    speak(talk)
    assert asked == [] and p.said == []


def test_the_conversation_ends_after_a_quiet_while():
    talk, p, asked, heard, clock = make(heard=["Hey Ari, hi", "what time is it"])
    speak(talk)
    p.busy = False
    talk.tick()
    clock.t += 21
    talk.tick()
    assert not talk.in_conversation
    speak(talk)
    assert asked == ["hi"]


def test_after_an_answer_a_video_or_long_talk_is_not_a_question():
    tv = ["The safety car is out and the field bunches up behind it going into the final sector of the lap"]
    talk, p, asked, heard, clock = make(heard=["Hey Ari, hi", "and the backups?", *tv],
                                        follow_ok=lambda text: len(text.split()) <= 12)
    speak(talk)
    p.busy = False
    talk.tick()
    speak(talk)
    assert asked == ["hi", "and the backups"]
    p.busy = False
    talk.tick()
    speak(talk)  # long commentary from the speakers, no "Hey Ari": ignored
    assert asked == ["hi", "and the backups"]


def test_a_yes_after_shall_i_counts_even_while_the_pc_plays_sound():
    heard = ["Hey Ari, delete the old logs", "yes"]
    clock, p, asked = Clock(), FakePlayer(), []

    def ask(q):
        asked.append(q)
        return {"reply": "Shall I?", "pending": {"kind": "action"}} if len(asked) == 1 else {"reply": "Done."}

    talk = vl.Talk(turns=vl.Turns(), transcribe=lambda a: heard.pop(0), wake_rest=wake_rest, ask=ask, player=p,
                   clock=clock, follow_ok=lambda text: False)  # the PC is playing a video
    speak(talk)
    p.busy = False
    talk.tick()
    speak(talk)
    assert asked == ["delete the old logs", "yes"]


def test_talking_over_ari_with_its_own_words_resumes():
    talk, p, asked, heard, clock = make(heard=["Hey Ari, the weather", "more detail"])
    speak(talk)
    assert p.busy
    events = speak(talk)
    assert "barge" in events and "resumed" in events
    assert p.log[-2:] == ["pause", "resume"] and asked == ["the weather"]


def test_mm_hmm_is_not_an_interruption():
    talk, p, asked, heard, clock = make(heard=["Hey Ari, the weather", "mm hmm"])
    speak(talk)
    assert "resumed" in speak(talk)


def test_a_real_interruption_stops_ari_and_is_the_next_question():
    talk, p, asked, heard, clock = make(heard=["Hey Ari, the weather", "no, I meant tomorrow"])
    speak(talk)
    speak(talk)
    assert "stop" in p.log and asked == ["the weather", "no, I meant tomorrow"]


def test_two_stage_wake_check_uses_the_small_model_first():
    heard_small = ["the news says"]
    talk, p, asked, heard, clock = make(heard=[])
    talk.transcribe_wake = lambda a: heard_small.pop(0)
    speak(talk)  # the small model heard no "Hey Ari": the big one is never asked
    assert asked == [] and heard_small == []


def test_talk_button_starts_a_conversation():
    talk, p, asked, heard, clock = make(heard=["what's running"])
    talk.wake()
    speak(talk)
    assert asked == ["what's running"]


def test_argus_down_is_said():
    talk, p, asked, heard, clock = make(heard=["Hey Ari, hi"])

    def boom(q):
        raise OSError("refused")

    talk.ask = boom
    speak(talk)
    assert "can't reach Argus" in p.said[0]


def test_words_and_echo():
    assert vl.BYE.match("thanks Ari") and vl.BYE.match("okay that's all") and vl.BYE.match("bye")
    assert not vl.BYE.match("thanks for the reminder, now add milk")
    assert vl.BACKCHANNEL.match("Yeah.") and not vl.BACKCHANNEL.match("yeah but tomorrow")
    assert vl.echo_of("the backup finished", "Your backup finished at nine.")
    assert not vl.echo_of("stop that", "Your backup finished at nine.")
    assert vl.sentences("Hi. The backup finished at nine. Two jobs failed!") == [
        "Hi. The backup finished at nine.", "Two jobs failed!"]


def test_echo_gate_learns_the_echo_and_gets_stricter():
    g = vl.EchoGate()
    echo = np.full(vl.FRAME, 0.05, dtype=np.float32)
    you = np.full(vl.FRAME, 0.3, dtype=np.float32)
    assert not any(g(echo, True) for _ in range(20))
    assert g(you, True)
    g.fooled()
    g.fooled()
    g.fooled()
    assert g.ratio > 4 and not g(np.full(vl.FRAME, 0.2, dtype=np.float32), True)
    assert g(echo, False)  # Ari quiet: everything through


class Out:
    def __init__(self):
        self.cb = None

    def __call__(self, rate, cb):
        self.cb, self.rate = cb, rate
        return self

    def pull(self, n):
        buf = np.zeros((n, 1), dtype=np.float32)
        self.cb(buf, n)
        return buf[:, 0]


def wait(cond):
    import time

    for _ in range(200):
        if cond():
            return
        time.sleep(0.01)
    raise AssertionError("timed out")


def test_player_speaks_sentences_pauses_resumes_and_stops():
    out, reports, clock = Out(), [], Clock()
    p = vl.Player(lambda text: (np.ones(1000, dtype=np.float32) * 0.5, 8000), out,
                  report=lambda ph, tx: reports.append(ph), clock=clock)
    p.say(["One.", "Two."])
    wait(lambda: not p._making)
    assert p.busy and out.rate == 8000 and reports == ["speaking"]
    assert out.pull(500).max() == 0.5
    p.pause()
    assert out.pull(500).max() == 0.0 and p.busy  # paused: silent, still has the rest to say
    p.resume()
    assert out.pull(3000).max() == 0.5
    out.pull(3000)
    assert not p._chunks
    clock.t += 1
    assert not p.busy
    p.poll()
    assert reports[-1] in ("done", "speaking")
    p.say(["Three."])
    wait(lambda: not p._making)
    p.stop()
    assert out.pull(500).max() == 0.0


def test_player_thinking_pulse_and_cue():
    out = Out()
    p = vl.Player(lambda text: None, out, rate=8000)
    p.thinking(True)
    assert np.abs(out.pull(4000)).max() == 0.0  # working on an answer is silent: no beeping
    p.thinking(False)
    assert np.abs(out.pull(4000)).max() == 0.0
    p.cue(vl.chime_samples(16000), 16000)
    assert np.abs(out.pull(4000)).max() > 0.1


def test_smart_turn_download_is_checked(tmp_path, monkeypatch):
    member = b"model bytes"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(vl.SMART_TURN_MEMBER, member)
    wheel = buf.getvalue()
    monkeypatch.setattr(vl, "SMART_TURN_WHEEL_SHA", hashlib.sha256(wheel).hexdigest())
    monkeypatch.setattr(vl, "SMART_TURN_SHA", hashlib.sha256(member).hexdigest())
    got = vl.ensure_smart_turn(tmp_path, fetch=lambda url: wheel)
    assert got and got.read_bytes() == member
    assert vl.ensure_smart_turn(tmp_path, fetch=lambda url: b"never fetched") == got
    monkeypatch.setattr(vl, "SMART_TURN_SHA", "0" * 64)
    assert vl.ensure_smart_turn(tmp_path / "x", fetch=lambda url: wheel) is None  # tampered: not used


def test_the_island_shows_the_words_as_you_say_them():
    shown = []
    talk, p, asked, heard, clock = make(heard=["Hey Ari, what's running?", "and the backups, and also the long list of "
                                                "everything else that I wanted to ask you about today"])
    talk.live_words = lambda a: "and the backups"
    talk.show = lambda phase, text: shown.append((phase, text))
    talk.run = lambda f: f()  # no thread
    talk.live_every_s = 0.2
    speak(talk)  # not in a conversation yet ("Hey Ari" needed): nothing is shown
    assert shown == [("thinking", "what's running")]  # what you said stays on show while Ari works
    shown.clear()
    talk.in_talk_until = 1e9
    for _ in range(round(1.0 / DT)):
        talk.frame(F, 0.95)
        clock.t += DT
    assert shown and shown[-1] == ("listening", "and the backups")
    assert len(shown) == 1  # the same words are not sent twice
    talk.live_words = lambda a: "word " * 40
    clock.t += 1
    for _ in range(round(0.5 / DT)):
        talk.frame(F, 0.95)
    assert shown[-1][1].startswith("… ") and len(shown[-1][1]) <= 72


def test_words_from_a_finished_turn_are_not_shown():
    shown = []
    talk, p, asked, heard, clock = make(heard=["thanks"])
    talk.in_talk_until = 1e9
    talk.show = lambda phase, text: shown.append(text)
    pending = []
    talk.run = pending.append
    talk.live_words = lambda a: "old words"
    talk.live_every_s = 0.1
    for _ in range(round(1.0 / DT)):
        talk.frame(F, 0.95)
        clock.t += DT
    assert len(pending) == 1
    talk._turn(F)  # the turn ended before the words were ready
    pending[0]()
    assert shown == []


def test_the_first_piece_is_short_so_speech_starts_sooner():
    long_first = "Your week's been busy, with three hundred and forty four jobs done and a lot of time saved."
    got = vl.sentences(long_first + " Nice.")
    assert got[0] == "Your week's been busy," and got[1].startswith("with three hundred")
    assert vl.sentences("Your week's been busy! 344 jobs done.") == ["Your week's been busy!", "344 jobs done."]
    assert vl.sentences("Okay. Done.") == ["Okay. Done."]  # a tiny first one is joined to the next
    assert vl.sentences("Short one, with a comma.") == ["Short one, with a comma."]


def test_a_silent_room_does_not_run_the_voice_model():
    runs = []
    q = vl.Quiet(lambda f: runs.append(1) or 0.9)
    quiet = np.full(vl.FRAME, 0.001, np.float32)
    loud = np.full(vl.FRAME, 0.05, np.float32)
    for _ in range(100):
        assert q(quiet) == 0.0
    assert runs == [] and q.share_skipped() == 1.0
    assert q(loud) == 0.9 and len(runs) == 1  # someone talks: the model scores it
    for _ in range(q.hang_frames):
        q(quiet)  # ... and the next moments too (a word's end)
    assert len(runs) == 1 + q.hang_frames
    q(quiet)
    assert len(runs) == 1 + q.hang_frames  # then quiet again


def test_the_model_is_let_go_while_you_are_away_and_loaded_when_you_are_back(monkeypatch):
    import threading as th

    from argus import ari_listen
    from argus.config import Config

    pings = []
    away = {"s": 0.0}
    cfg = Config.model_validate({"models": {"tiers": {"T1": {"provider": "ollama", "model": "qwen3"}},
                                            "chain": ["T1"]}, "ari": {"rest_after_min": 1}})
    started = []
    monkeypatch.setattr(th.Thread, "start", lambda self: started.append(self))
    sleeps = []

    def fake_sleep(s):  # passed in, not patched globally: other tests' threads sleep too
        sleeps.append(s)
        if len(sleeps) == 1:
            away["s"] = 120  # gone for two minutes
        elif len(sleeps) == 3:
            away["s"] = 0  # back
        elif len(sleeps) == 4:
            raise StopIteration

    ari_listen.keep_warm(cfg, every=1000, check=0, away=lambda: away["s"], sleep=fake_sleep, post=pings.append)
    loop = next(t for t in started if t.name == "keep-warm")._target  # not another test's thread
    try:
        loop()
    except StopIteration:
        pass
    assert len(pings) == 2  # at the start, then not while away, then at once when back
