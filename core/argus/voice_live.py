"""Talking with Ari like a conversation, on the PC (ari_listen uses this).

What modern voice agents do (Pipecat, LiveKit Agents, OpenAI Realtime), done locally:

1. Voice activity: Silero VAD (the ONNX model faster-whisper ships) scores every 32 ms of sound; speech starts after
   0.2 s of voice. Without it, a loudness gate does the same job, less well.
2. End of turn: a short pause doesn't mean you're done. When you pause, Smart Turn v3 (an 8 MB model that listens
   to how the sentence sounds) says whether you finished; if not, Ari waits (up to 2.5 s of silence) so you can
   finish the thought. Without the model, 0.8 s of silence ends the turn.
3. Conversation mode: after "Hey Ari" you just talk back and forth; it ends after 20 s of quiet, or when you say
   "thanks Ari" / "that's all" / "bye".
4. Barge-in: while Ari talks, your voice (clearly louder than the speakers' echo, for 0.3 s) pauses it at once.
   What you said is written down: Ari's own words coming back, or just "mm" / "yeah", and it carries on where it
   paused (false-interruption recovery); anything else stops it, and what you said is the next turn.
5. Sentence by sentence: the answer is spoken a sentence at a time, the first one as soon as it is ready.
6. No beeping while an answer takes a while: a short spoken "hmm, let me check" at most.

Everything here is plain logic with the audio, models and Argus passed in, so it can be tested without sound.
"""

from __future__ import annotations

import hashlib
import io
import logging
import random
import re
import threading
import time
import urllib.request
import zipfile
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .expressive import phrases

log = logging.getLogger("argus.voice_live")
RATE = 16000
FRAME = 512  # 32 ms: what Silero scores at a time

# ------------------------------------------------------------------ voice activity (Silero)


class Silero:
    """Silero VAD, one 32 ms frame at a time (state kept between frames). Takes faster-whisper's
    silero_vad_v6.onnx (inputs input/h/c) or the classic silero_vad.onnx (input/state/sr)."""

    def __init__(self, path: str | Path):
        import numpy as np
        import onnxruntime as ort  # type: ignore[import-not-found]

        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.log_severity_level = 4
        self.s = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"], sess_options=opts)
        self.v6 = "h" in {i.name for i in self.s.get_inputs()}
        self.np = np
        self.reset()

    def reset(self) -> None:
        np = self.np
        self.ctx = np.zeros((1, 64), dtype=np.float32)
        self.h = np.zeros((1, 1, 128), dtype=np.float32)
        self.c = np.zeros((1, 1, 128), dtype=np.float32)
        self.state = np.zeros((2, 1, 128), dtype=np.float32)

    def __call__(self, frame) -> float:
        np = self.np
        x = np.concatenate([self.ctx, np.asarray(frame, dtype=np.float32).reshape(1, FRAME)], axis=1)
        if self.v6:
            out, self.h, self.c = self.s.run(None, {"input": x, "h": self.h, "c": self.c})
        else:
            out, self.state = self.s.run(None, {"input": x, "state": self.state, "sr": np.array(RATE, dtype=np.int64)})
        self.ctx = x[:, -64:]
        return float(np.asarray(out).reshape(-1)[0])


def silero_path() -> Path | None:
    """faster-whisper's copy of the Silero model (installed with Ari's hearing)."""
    try:
        import faster_whisper  # type: ignore[import-not-found]
    except ImportError:
        return None
    p = Path(faster_whisper.__file__).parent / "assets" / "silero_vad_v6.onnx"
    return p if p.exists() else None


class Quiet:
    """In front of the voice model: a plainly silent frame (not much above the room's background) is scored 0
    without running the model, so a quiet room costs next to nothing. After anything louder the model runs for a
    while (`hang_s`), so a word's start and end are still scored by it."""

    def __init__(self, vad: Callable[[Any], float], ratio: float = 2.5, least: float = 0.002, hang_s: float = 0.6):
        self.vad, self.ratio, self.least = vad, ratio, least
        self.floor = 0.004
        self.hang_frames = round(hang_s * RATE / FRAME)
        self.hang = 0
        self.ran = self.skipped = 0

    def __call__(self, frame) -> float:
        import numpy as np

        rms = float(np.sqrt(np.mean(np.square(frame)))) if len(frame) else 0.0
        # the background: follows quiet quickly, noise only slowly (a fan coming on)
        self.floor = 0.9 * self.floor + 0.1 * rms if rms < self.floor else 0.999 * self.floor + 0.001 * rms
        if rms >= max(self.floor * self.ratio, self.least):
            self.hang = self.hang_frames
        elif self.hang > 0:
            self.hang -= 1
        else:
            self.skipped += 1
            return 0.0
        self.ran += 1
        return self.vad(frame)

    def share_skipped(self) -> float:
        """How much of the time the model was not needed (since last asked)."""
        n = self.ran + self.skipped
        out = self.skipped / n if n else 0.0
        self.ran = self.skipped = 0
        return out


class LoudnessVad:
    """Without Silero: louder than the room's background (learned as it goes) counts as voice."""

    def __init__(self, ratio: float = 3.0):
        self.floor, self.ratio = 0.004, ratio

    def __call__(self, frame) -> float:
        import numpy as np

        rms = float(np.sqrt(np.mean(np.square(frame)))) if len(frame) else 0.0
        level = max(self.floor * self.ratio, 0.01)
        if rms < level:
            self.floor = 0.98 * self.floor + 0.02 * rms
        return min(1.0, rms / (2 * level))


# ------------------------------------------------------------------ end of turn (Smart Turn v3)

SMART_TURN_WHEEL = ("https://files.pythonhosted.org/packages/10/fe/566fd73f43e66ce48b9a7e5dfa9cf79c713184978681708ac5c109c"
                    "233ee/pipecat_ai-1.12.0-py3-none-any.whl")
SMART_TURN_WHEEL_SHA = "4cd3dc071b7b64da7ac700a26a224b773ae6ef8d6694a4566332d2e5e39ed6d9"
SMART_TURN_MEMBER = "pipecat/audio/turn/smart_turn/data/smart-turn-v3.2-cpu.onnx"
SMART_TURN_SHA = "2bb026316b14a660486a75b1733cd3fbab8c2fd0314dc9af7be49f8cca967e4f"


def ensure_smart_turn(models: Path, fetch: Callable[[str], bytes] | None = None) -> Path | None:
    """The Smart Turn v3.2 model (BSD 2-Clause, by Daily), taken once from the pipecat-ai wheel on PyPI and checked
    by SHA-256. None when it can't be had (the turn then ends on silence alone)."""
    out = models / "smart-turn-v3.2-cpu.onnx"
    if out.exists() and hashlib.sha256(out.read_bytes()).hexdigest() == SMART_TURN_SHA:
        return out
    try:
        if fetch is None:
            with urllib.request.urlopen(SMART_TURN_WHEEL, timeout=120) as r:  # noqa: S310 - a fixed https URL
                wheel = r.read()
        else:
            wheel = fetch(SMART_TURN_WHEEL)
        if hashlib.sha256(wheel).hexdigest() != SMART_TURN_WHEEL_SHA:
            log.warning("smart turn download didn't match its checksum; not used")
            return None
        data = zipfile.ZipFile(io.BytesIO(wheel)).read(SMART_TURN_MEMBER)
        if hashlib.sha256(data).hexdigest() != SMART_TURN_SHA:
            return None
        models.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
        log.info("smart turn model saved", extra={"path": str(out)})
        return out
    except Exception as e:  # noqa: BLE001 - optional: offline, PyPI down, ...
        log.warning("no smart turn model", extra={"error": str(e)[:200]})
        return None


class SmartTurn:
    """Did the speaker finish? (the last 8 s of their audio -> probability)."""

    def __init__(self, path: str | Path):
        import onnxruntime as ort  # type: ignore[import-not-found]

        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.log_severity_level = 4
        self.s = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"], sess_options=opts)

    def __call__(self, audio) -> float:
        import numpy as np

        from .whisper_features import compute_whisper_log_mel_features

        a = np.asarray(audio, dtype=np.float32)
        n = 8 * RATE
        a = a[-n:] if len(a) > n else np.pad(a, (n - len(a), 0))
        feats = compute_whisper_log_mel_features(a, do_normalize=True)[None, ...]
        return float(np.asarray(self.s.run(None, {"input_features": feats})[0]).reshape(-1)[0])


# ------------------------------------------------------------------ turns: who is talking, when you're done


@dataclass
class Turns:
    """Feed it every frame with its voice score; it says when you start, when you finished a turn (with the audio),
    and when you talk over Ari. No audio devices or models in here."""

    finished: Callable[[Any], float] | None = None  # Smart Turn: audio -> P(finished); None: silence decides
    start_s: float = 0.2  # voice this long = speech started
    pause_s: float = 0.2  # a pause this long: ask Smart Turn
    max_pause_s: float = 2.5  # silence this long ends the turn whatever Smart Turn says
    no_model_pause_s: float = 0.8  # without Smart Turn
    max_turn_s: float = 15.0
    pre_s: float = 0.4  # audio kept from just before the voice started
    on: float = 0.5  # voice score that counts as voice
    off: float = 0.35  # ... and as silence
    barge_on: float = 0.8  # while Ari talks: a stricter score (its own voice comes back through the mic)
    barge_s: float = 0.3
    _pre: deque = field(default_factory=lambda: deque(maxlen=13))
    _buf: list = field(default_factory=list)
    _voice_s: float = 0.0
    _quiet_s: float = 0.0
    _talking: bool = False
    _asked_at_quiet: float = -1.0

    def reset(self) -> None:
        self._buf, self._voice_s, self._quiet_s, self._talking, self._asked_at_quiet = [], 0.0, 0.0, False, -1.0
        self._pre.clear()

    @property
    def talking(self) -> bool:
        return self._talking

    def feed(self, frame, voice: float, ari_talking: bool = False) -> tuple[str, Any] | None:
        """-> ("start", None) | ("barge", None) | ("end", audio) | None."""
        dt = FRAME / RATE
        on = self.barge_on if ari_talking and not self._talking else self.on
        if not self._talking:
            self._pre.append(frame)
            self._voice_s = self._voice_s + dt if voice >= on else 0.0
            need = self.barge_s if ari_talking else self.start_s
            if self._voice_s >= need:
                self._talking, self._buf, self._quiet_s, self._asked_at_quiet = True, list(self._pre), 0.0, -1.0
                self._pre.clear()
                return ("barge", None) if ari_talking else ("start", None)
            return None
        self._buf.append(frame)
        self._quiet_s = self._quiet_s + dt if voice < self.off else 0.0
        if self._quiet_s == 0.0:
            self._asked_at_quiet = -1.0
        if len(self._buf) * dt >= self.max_turn_s:
            return self._end()
        if self.finished is None:
            return self._end() if self._quiet_s >= self.no_model_pause_s else None
        if self._quiet_s >= self.max_pause_s:
            return self._end()
        if self._quiet_s >= self.pause_s and self._asked_at_quiet < 0:
            self._asked_at_quiet = self._quiet_s  # ask once per pause
            import numpy as np

            try:
                done = self.finished(np.concatenate(self._buf)) >= 0.5
            except Exception as e:  # noqa: BLE001 - fall back to silence
                log.warning("smart turn failed", extra={"error": str(e)[:200]})
                done = self._quiet_s >= self.no_model_pause_s
            if done:
                return self._end()
        return None

    def so_far(self, seconds: float = 10.0) -> Any:
        """The last `seconds` of what you are saying now (for the live words on the island)."""
        import numpy as np

        keep = max(1, round(seconds * RATE / FRAME))
        return np.concatenate(self._buf[-keep:]) if self._buf else np.zeros(0, dtype=np.float32)

    def _end(self) -> tuple[str, Any]:
        import numpy as np

        audio = np.concatenate(self._buf) if self._buf else np.zeros(0, dtype=np.float32)
        self._buf, self._talking, self._voice_s, self._quiet_s = [], False, 0.0, 0.0
        return ("end", audio)


# ------------------------------------------------------------------ what was said, in a conversation

BYE = re.compile(r"^\W*(?:(?:ok(?:ay)?|thanks?|thank you|cheers)[\s,]*(?:ari)?[\s,.!]*)?(?:that'?s all|that is all|"
                 r"bye|good ?bye|see you|that'?ll be all|nothing else|i'?m done|stop listening)\b|"
                 r"^\W*(?:thanks?|thank you)(?:[\s,]+ari)?\W*$", re.I)
_BC = r"(?:mm+|hmm+|uh[- ]?huh|mhm|yeah|yep|yes|ok(?:ay)?|right|sure|i see|got it|cool|nice|aha|oh)"
BACKCHANNEL = re.compile(rf"^\W*{_BC}(?:[\s,.]+{_BC})*\W*$", re.I)


def _words(s: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", s.lower())


def echo_of(heard: str, speaking: str) -> bool:
    """Is what the mic heard just Ari's own words coming back? (most of its words are in what Ari is saying)"""
    h, s = _words(heard), set(_words(speaking))
    return bool(h) and sum(w in s for w in h) / len(h) >= 0.6


def sentences(text: str) -> list[str]:
    """Split an answer to be spoken a sentence at a time. The first piece stays short (it decides how soon Ari
    starts talking): a long first sentence is cut at its first comma, and only a tiny one ("Hi.") is joined to the
    next. Later short bits are kept with the next one (a voice reading every few words sounds choppy)."""
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])", text.strip()) if p.strip()]
    out: list[str] = []
    for p in parts:
        if out and len(out[-1]) < (10 if len(out) == 1 else 25):
            out[-1] = f"{out[-1]} {p}"
        else:
            out.append(p)
    if out and len(out[0]) > 60:
        m = re.search(r"[,;:\u2014]\s+", out[0][18:])  # the first comma after at least 18 characters
        if m and 18 + m.end() < len(out[0]) - 12:
            cut = 18 + m.end()
            out[0:1] = [out[0][:cut].strip(), out[0][cut:].strip()]
    return out or ([text.strip()] if text.strip() else [])


def tagged(ps: list[tuple[str, str]]) -> list[str]:
    """Phrases as sentences to speak, each with the mood it is said in ("[calm] ...")."""
    return [f"[{m}] {p}" if m != "neutral" else p for m, w in ps for p in sentences(w)]


def _takes_partial(fn: Callable) -> bool:
    import inspect

    try:
        return len(inspect.signature(fn).parameters) >= 2
    except (TypeError, ValueError):
        return False


def after_spoken(final: str, spoken: str) -> str | None:
    """What of the final reply is left once `spoken` (its start, streamed earlier) has been said; None when the final
    reply doesn't start that way (it was written again). Letters and digits are compared; tags, punctuation and
    spaces are ignored."""
    want = [c for c in re.sub(r"\[[^\]]*\]", "", spoken).lower() if c.isalnum()]
    if not want:
        return None
    k = 0
    i = 0
    text = final
    while i < len(text) and k < len(want):
        if text[i] == "[":  # a tag: skip it
            j = text.find("]", i)
            if j > 0:
                i = j + 1
                continue
        c = text[i].lower()
        if c.isalnum():
            if c != want[k]:
                return None
            k += 1
        i += 1
    if k < len(want):
        return None
    while i < len(text) and not text[i].isalnum() and text[i] != "[":
        i += 1  # the sentence end that was said too
    return text[i:].strip()


@dataclass
class Talk:
    """The conversation: wake phrase, back and forth without it, barge-in, ending. Uses `Turns` for the audio, and
    is handed everything that touches the world (so it can be tested without sound)."""

    turns: Turns
    transcribe: Callable[[Any], str]  # the good model (ari.whisper_model)
    wake_rest: Callable[[str], str | None]  # text -> what follows "Hey Ari", or None
    ask: Callable[[str], dict]  # text -> Ari's answer {reply, pending}
    player: Any  # .say(sentences), .pause(), .resume(), .stop(), .busy (bool), .text (what it's saying)
    report: Callable[[str], None] = lambda phase: None
    chime: Callable[[], None] = lambda: None
    clock: Callable[[], float] = time.monotonic
    idle_s: float = 20.0  # conversation ends after this long without you speaking
    live: bool = True  # False: every request needs "Hey Ari" (except right after "Shall I?")
    transcribe_wake: Callable[[Any], str] | None = None  # the small model, for "is this for Ari?" (first 3 s)
    on_false_barge: Callable[[], None] = lambda: None  # it was Ari's own voice: be harder to interrupt
    in_talk_until: float = 0.0
    live_words: Callable[[Any], str] | None = None  # the small model: audio so far -> words (shown while you talk)
    show: Callable[[str, str], None] | None = None  # (phase, text): the island's words
    live_every_s: float = 1.0
    run: Callable[[Callable[[], None]], None] = lambda f: threading.Thread(target=f, daemon=True).start()
    _live_at: float = 0.0
    _live_busy: bool = False
    _live_text: str = ""
    _gen: int = 0
    _following: bool = False  # "following" was reported for this answer (the island's follow-up ring)
    paused_for_barge: bool = False
    _lock: Any = field(default_factory=threading.RLock)

    @property
    def in_conversation(self) -> bool:
        return self.clock() < self.in_talk_until

    def wake(self) -> None:
        """The island's Talk button: listen now, no wake phrase needed."""
        with self._lock:
            self.player.stop()
            self.chime()
            self.report("listening")
            self._keep_talking(pending=True)

    def frame(self, frame, voice: float) -> str | None:
        with self._lock:
            return self._frame(frame, voice)

    def _frame(self, frame, voice: float) -> str | None:
        ev = self.turns.feed(frame, voice, ari_talking=self.player.busy and not self.paused_for_barge)
        if ev is None:
            self._live()
            return None
        kind, audio = ev
        if kind == "barge":
            self.player.pause()  # stop talking the moment you do; decide below once we know what you said
            self.paused_for_barge = True
            self.report("listening")
            return "barge"
        if kind == "start":
            if self.in_conversation:
                self.report("listening")
            return "start"
        return self._turn(audio)

    def _live(self) -> None:
        """While you talk (in a conversation), now and then hear what you've said so far and put it on the island."""
        if self.live_words is None or self.show is None or not self.turns.talking:
            return
        if not (self.in_conversation or self.paused_for_barge) or self._live_busy:
            return
        now = self.clock()
        if now - self._live_at < self.live_every_s:
            return
        self._live_at, self._live_busy = now, True
        audio, gen = self.turns.so_far(), self._gen

        def work() -> None:
            try:
                text = " ".join((self.live_words(audio) or "").split())  # type: ignore[misc]
                if gen == self._gen and self.turns.talking and text and text != self._live_text:
                    self._live_text = text
                    tail = text if len(text) <= 70 else "… " + text[-68:].split(" ", 1)[-1]
                    self.show("listening", tail)  # type: ignore[misc]
            except Exception as e:  # noqa: BLE001 - the words are a nicety
                log.info("live words failed", extra={"error": str(e)[:100]})
            finally:
                self._live_busy = False

        self.run(work)

    def _turn(self, audio) -> str | None:
        self._gen += 1  # words still being worked out for this turn are no longer wanted
        self._live_text = ""
        barged, self.paused_for_barge = self.paused_for_barge, False
        if not (barged or self.in_conversation) and self.transcribe_wake is not None:
            head = (self.transcribe_wake(audio[: 3 * RATE]) or "").strip()  # quick look: was it "Hey Ari …"?
            if self.wake_rest(head) is None:
                if head:
                    log.info("heard, no wake phrase", extra={"text": head[:80]})
                return None
        text = (self.transcribe(audio) or "").strip()
        if barged:
            if not text or echo_of(text, self.player.text):
                self.on_false_barge()
                self.player.resume()  # its own voice (or a cough): carry on where it paused
                return "resumed"
            if BACKCHANNEL.match(text):
                self.player.resume()  # "mm", "yeah": you're listening, not interrupting
                return "resumed"
            self.player.stop()
        if not text:
            return None
        rest = self.wake_rest(text)
        if rest is None and not (self.in_conversation or barged or self.transcribe_wake is not None):
            return None  # not for Ari
        command = (rest if rest is not None else text).strip(" ,.!?")
        if (self.in_conversation or barged) and BYE.match(text):  # "thanks Ari": not a wake phrase
            command = text
        if not command:  # "Hey Ari" alone: listening
            self.chime()
            self.report("listening")
            self._following = True  # waiting for the command: plain "listening", not the follow-up ring
            self._keep_talking(pending=True)
            return "wake"
        if BYE.match(command):
            self.in_talk_until = 0.0
            self.player.say(["Okay."])
            self.report("idle")
            return "bye"
        self._following = False
        if self.show is not None:
            self.show("thinking", command)  # the island keeps what you said on show while Ari works
        else:
            self.report("thinking")
        log.info("heard", extra={"text": command})
        streamed = {"text": "", "mood": "neutral"}

        def partial(text: str, mood: str) -> None:  # a reply still being written: its finished sentences now
            if not text.startswith(streamed["text"]):
                return
            new = text[len(streamed["text"]):].strip()
            if not new:
                return
            ps = phrases(new, mood if not streamed["text"] else streamed["mood"])  # tags in the text win
            streamed["text"] = text
            if ps:
                streamed["mood"] = ps[-1][0]
            self.player.add(tagged(ps))

        try:
            ans = self.ask(command, partial) if _takes_partial(self.ask) else self.ask(command)
        except Exception as e:  # noqa: BLE001 - argusd down: say so, keep listening
            log.warning("could not reach Ari", extra={"error": str(e)[:200]})
            ans = {"reply": "Sorry, I can't reach Argus right now."}
        reply = (ans.get("reply") or "").strip()
        rest = after_spoken(reply, streamed["text"]) if streamed["text"] else None
        if rest is not None:  # most of it is said already: the rest follows on
            self.player.add(tagged(phrases(rest, streamed["mood"])))
        elif reply:
            # "[excited] We won! [sympathetic] Shame about the rain.": each sentence carries the mood it is said in
            self.player.say(tagged(phrases(reply)))
        self._keep_talking(pending=bool(ans.get("pending")))
        return command

    def _keep_talking(self, pending: bool = False) -> None:
        if self.live or pending:
            self.in_talk_until = self.clock() + self.idle_s + 30.0  # the reply's own length is added by tick()

    def tick(self) -> None:
        """Call often: the quiet clock only runs once Ari has finished talking; at the end, back to waiting."""
        now = self.clock()
        if self.player.busy or self.turns.talking:
            if self.in_talk_until:
                self.in_talk_until = max(self.in_talk_until, now + self.idle_s)
            return
        if self.in_talk_until and self.in_talk_until > now + self.idle_s:
            self.in_talk_until = now + self.idle_s
        if self.in_talk_until and not self._following:  # answered: still listening, no "Hey Ari" needed
            self._following = True
            self.report("following")
        if self.in_talk_until and now >= self.in_talk_until:
            self.in_talk_until = 0.0
            self._following = False
            self.report("idle")


# ------------------------------------------------------------------ telling you from Ari's own voice


class EchoGate:
    """While Ari talks, its voice comes back through the mic and Silero rightly calls it speech. This learns how
    loud that echo is (the median of the last 1.5 s while Ari talks) and only lets through sound clearly louder:
    you, talking over it. Each time it was fooled anyway (the words were Ari's own), it gets a bit stricter.
    With headphones there is no echo and any voice gets through."""

    def __init__(self, ratio: float = 2.5, least: float = 0.01):
        self.ratio, self.least = ratio, least
        self._levels: deque = deque(maxlen=47)

    def __call__(self, frame, ari_talking: bool) -> bool:
        if not ari_talking:
            self._levels.clear()
            return True
        import numpy as np

        rms = float(np.sqrt(np.mean(np.square(frame)))) if len(frame) else 0.0
        echo = float(np.median(self._levels)) if len(self._levels) >= 8 else rms
        self._levels.append(rms)
        return len(self._levels) >= 8 and rms > max(self.least, self.ratio * echo)

    def fooled(self) -> None:
        self.ratio = min(self.ratio * 1.25, 6.0)


# ------------------------------------------------------------------ speaking: sentence by sentence, can pause


class Player:
    """Speaks an answer a sentence at a time: the first sentence plays while the next is being made. Pause and
    resume (for barge-in), stop, a soft "thinking" pulse and short cues (the chime). The sound card is reached
    through `open_stream(rate, callback)` (sounddevice in real life), so all of this can be tested without one.

    `synth(text)` -> (samples float32, rate) or None. `report(phase, text)` is told "speaking" and "done"."""

    TAIL_S = 0.35  # the room still rings this long after the last word: not you talking yet

    def __init__(self, synth: Callable[[str], tuple[Any, int] | None], open_stream: Callable[[int, Callable], Any],
                 report: Callable[[str, str], None] = lambda phase, text: None, rate: int = 22050,
                 clock: Callable[[], float] = time.monotonic, show: Callable[[str], None] = lambda text: None):
        import numpy as np

        self.np, self.synth, self.open_stream, self.report, self.clock, self.show = (
            np, synth, open_stream, report, clock, show)
        self.rate = rate
        self._stream: Any = None
        self._lock = threading.Lock()
        self._open_lock = threading.Lock()
        self._chunks: deque = deque()  # sound waiting to play (float32 arrays at self.rate)
        self._pos = 0
        self._cues: deque = deque()  # chime etc.: played over everything, never paused
        self._gen = 0  # bumped by say()/stop(): an older answer still being made is dropped
        self._making = False
        self._more: list[str] = []  # sentences added while the earlier ones are still being made (add())
        self._paused = False
        self._thinking = False
        self._think_t = 0
        self._ended_at = 0.0
        self._began = time.perf_counter()
        self._was_busy = False
        self.text = ""

    # --- the sound card's side (called every few ms from its thread)
    def callback(self, outdata, frames, t=None, status=None) -> None:  # noqa: ARG002 - sounddevice's signature
        np = self.np
        out = np.zeros(frames, dtype=np.float32)
        with self._lock:
            if not self._paused and not self._cues:  # a cue (the chime, "hmm, let me check") first
                n = 0
                while n < frames and self._chunks:
                    c = self._chunks[0]
                    take = min(frames - n, len(c) - self._pos)
                    out[n:n + take] = c[self._pos:self._pos + take]
                    n += take
                    self._pos += take
                    if self._pos >= len(c):
                        self._chunks.popleft()
                        self._pos = 0
                if n and not self._chunks and not self._making:
                    self._ended_at = self.clock()
            m = 0
            while m < frames and self._cues:
                c = self._cues[0]
                take = min(frames - m, len(c))
                out[m:m + take] += c[:take]
                m += take
                if take < len(c):
                    self._cues[0] = c[take:]
                else:
                    self._cues.popleft()
        outdata[:, 0] = out

    # --- Ari's side
    @property
    def busy(self) -> bool:
        """Talking (or about to, or the room still ringing). False while paused only to the sound card."""
        with self._lock:
            if self._chunks or self._making or self._cues:
                return True
            return self.clock() - self._ended_at < self.TAIL_S

    def _ensure(self, rate: int) -> None:
        with self._open_lock:
            self._ensure_locked(rate)

    def _ensure_locked(self, rate: int) -> None:
        if self._stream is not None and rate == self.rate:
            return
        if self._stream is not None:
            try:
                self._stream.close()
            except Exception:  # noqa: BLE001
                pass
        self.rate = rate
        self._stream = self.open_stream(rate, self.callback)

    def say(self, parts: list[str]) -> None:
        """Speak these sentences in order (stops whatever was being said)."""
        parts = [p for p in parts if p.strip()]
        thinking = self._thinking
        self.stop()
        if not parts:
            return
        with self._lock:
            self._thinking = thinking  # the pulse goes on until the first sentence is ready
            self._gen += 1
            gen = self._gen
            self._making = True
            self.text = " ".join(parts)
        self.show(self.text)
        self._began = time.perf_counter()
        threading.Thread(target=self._make, args=(gen, parts), daemon=True, name="ari-voice").start()

    def add(self, parts: list[str]) -> None:
        """Speak these sentences after what is being said now, without stopping it (a reply arriving in pieces)."""
        parts = [p for p in parts if p.strip()]
        if not parts:
            return
        with self._lock:
            going = self._making or bool(self._chunks)
            self.text = f"{self.text} {' '.join(parts)}".strip() if going else " ".join(parts)
            if self._making:
                self._more.extend(parts)
                start = False
            else:
                self._making, start, gen = True, True, self._gen
        self.show(self.text)
        if start:
            self._began = time.perf_counter()
            threading.Thread(target=self._make, args=(gen, parts), daemon=True, name="ari-voice").start()

    def _make(self, gen: int, parts: list[str]) -> None:
        np = self.np
        first = True
        try:
            while True:
                for part in parts:
                    t0 = time.perf_counter()
                    got = self.synth(part)
                    if got is None:
                        continue
                    log.info("voice made", extra={"chars": len(part), "ms": int((time.perf_counter() - t0) * 1000),
                                                  "since_start_ms": int((time.perf_counter() - self._began) * 1000),
                                                  "first": first})
                    audio, rate = got
                    with self._lock:
                        if gen != self._gen:
                            return
                    self._ensure(rate)
                    with self._lock:
                        if gen != self._gen:
                            return
                        self._thinking = False
                        gap = np.zeros(int(0.12 * rate) if (self._chunks or not first) else 0, dtype=np.float32)
                        self._chunks.append(np.concatenate([gap, np.asarray(audio, dtype=np.float32)]))
                    if first:
                        self.report("speaking", self.text)
                        first = False
                with self._lock:
                    if gen != self._gen:
                        return
                    if not self._more:  # done: in the same lock as add() looks, so nothing added is lost
                        self._making, self._thinking = False, False
                        if not self._chunks:
                            self._ended_at = self.clock()
                        return
                    parts, self._more = self._more, []
        except Exception as e:  # noqa: BLE001 - the voice failing must not stop Ari listening
            log.warning("speaking failed", extra={"error": str(e)[:200]})
        finally:
            with self._lock:
                if gen == self._gen and self._making:  # it failed part way
                    self._making, self._thinking = False, False
                    self._more = []
                    if not self._chunks:
                        self._ended_at = self.clock()

    def pause(self) -> None:
        with self._lock:
            self._paused = True

    def resume(self) -> None:
        with self._lock:
            self._paused = False

    def stop(self) -> None:
        with self._lock:
            self._gen += 1
            had = bool(self._chunks) or self._making
            self._chunks.clear()
            self._more = []
            self._pos, self._making, self._paused, self._thinking = 0, False, False, False
            if had:
                self._ended_at = self.clock()

    def thinking(self, on: bool) -> None:
        """Ari is working on an answer (silent: no beeping; the Helios pill and the spoken filler show it)."""
        if on:
            self._ensure(self.rate)
        with self._lock:
            self._thinking, self._think_t = on, 0

    def cue(self, samples, rate: int) -> None:
        """A short sound over everything (the chime)."""
        np = self.np
        self._ensure(self.rate)
        a = np.asarray(samples, dtype=np.float32)
        if rate != self.rate:
            a = np.interp(np.arange(0, len(a), rate / self.rate), np.arange(len(a)), a).astype(np.float32)
        with self._lock:
            self._cues.append(a)

    def filler(self, samples, rate: int) -> None:
        """A short spoken filler while Ari works ("hmm, let me check"): like a cue, then the soft pulse."""
        self.cue(samples, rate)
        with self._lock:
            self._thinking, self._think_t = True, 0

    def poll(self) -> None:
        """Call often: tells Argus when Ari has finished talking."""
        busy = self.busy
        if self._was_busy and not busy:
            self.report("done", self.text)
        self._was_busy = busy


def chime_samples(rate: int = RATE):
    """Two soft rising notes."""
    import numpy as np

    t = np.linspace(0, 0.12, int(rate * 0.12), endpoint=False)
    fade = np.minimum(1.0, np.minimum(t, 0.12 - t) / 0.01)
    return (np.concatenate([np.sin(2 * np.pi * 880 * t) * fade, np.sin(2 * np.pi * 1320 * t) * fade]) * 0.2
            ).astype(np.float32)


def wav_samples(data: bytes) -> tuple[Any, int]:
    import wave

    import numpy as np

    with wave.open(io.BytesIO(data)) as w:
        a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
        return a, w.getframerate()


FILLERS = ["[calm] Hmm, let me check.", "[calm] One sec.", "[calm] Okay, give me a moment.",
           "[calm] Mm, let me see.", "[cheerful] On it."]


class Fillers:
    """A few short fillers, made once in the background with Ari's voice, so one plays at once when an answer takes a
    moment (better than silence or a tone). `synth(text)` -> (samples, rate) or None."""

    def __init__(self, synth, texts: list[str] | None = None, rng: random.Random | None = None):
        self.ready: list[tuple] = []
        self.rng = rng or random.Random()
        self._last = -1
        threading.Thread(target=self._make, args=(synth, texts or FILLERS), daemon=True, name="fillers").start()

    def _make(self, synth, texts, tries: int = 10, wait: float = 30) -> None:
        for _ in range(tries):  # Argus may still be starting
            for t in texts:
                try:
                    got = synth(t)
                except Exception:  # noqa: BLE001 - no voice yet: the pulse alone
                    got = None
                if got is not None:
                    self.ready.append(got)
            if self.ready:
                return
            time.sleep(wait)

    def pick(self):
        if not self.ready:
            return None
        i = self.rng.randrange(len(self.ready))
        if i == self._last and len(self.ready) > 1:
            i = (i + 1) % len(self.ready)
        self._last = i
        return self.ready[i]
