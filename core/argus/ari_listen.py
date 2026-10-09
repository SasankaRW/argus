"""Ari on the PC: "Hey Ari" on the microphone, without a browser and without sending sound anywhere.

    python -m argus.ari_listen            (dev.ps1 up starts it when ari.listen is on)

How it hears (voice_live.py has the details):
1. Silero VAD finds speech in the microphone stream and Smart Turn says when you've finished a sentence.
2. The first 3 s of what you said go to a small Whisper model (`ari.listen_wake_model`) with no hints, to see
   whether it starts with "Hey Ari" (or a wake-word model, `ari.wake_model`, decides that). Nothing leaves the PC.
3. The request is written down by the better model (`ari.whisper_model`), biased towards the names Ari expects.
   Whisper reading those names back on noise is thrown away.
4. It goes to Ari (POST /ari, the same chat as Helios) and the answer is spoken in Ari's one voice (the voice
   server on this PC) or, if it can't speak, shown on the island.
5. For a few seconds after an answer (`ari.talk_idle_s`) you can carry on without "Hey Ari", but not while the PC
   is playing sound (a video's voices aren't you) and not with a long stream of words (a call, the TV).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

log = logging.getLogger("argus.ari_listen")
RATE = 16000
BLOCK = 480  # 30 ms


STRONG = "ari|arie|arri|aree|ary|aari|argus"
# How Whisper (tiny) writes "Ari" after "hey": close enough to count even while the PC plays sound (from your logs:
# "Hey, Adi", "Hey, Addie", "Hey Yaddie")
CLOSE = "adi|addie|addy|ady|aidy|aidi|yaddie|yadi|airy|arri|ahri|aree|aries|ari's"
WEAK = "harry|hari|hurry|siri|sorry|audi|ori|aria|ali|ally|arty|artie|lorry|eddie|edie"  # further off: not strict
_LEAD = r"^(?:(?:uh|um|erm|so|oh|yeah|and|well|okay|hello|hi|hey)[\s,.]+){0,2}"  # "um, hello, hey Ari": fillers
# The wake phrase only at the START of what was heard: "Hey Ari …", "OK Harry …" (a mishearing), or "Ari, …". A
# name in the middle ("I was talking to Harry", a video saying "Argus") is not for Ari. That was most false starts.
WAKE_ANY = re.compile(rf"{_LEAD}(?:(?:hey|hi|hay|ok|okay)[\s,.]*(?:{STRONG}|{CLOSE}|{WEAK})|(?:{STRONG}))\b[\s,.!?]*",
                      re.I)
WAKE_STRICT = re.compile(rf"{_LEAD}(?:hey|hi|hay|ok|okay)[\s,.]*(?:{STRONG}|{CLOSE})\b[\s,.!?]*", re.I)
STRICT = {"on": False}  # the PC is playing sound (a video, music): only a clear "Hey Ari" counts (PlaybackGuard)


def wake_rest(text: str, strict: bool | None = None) -> str | None:
    """What follows the wake phrase, or None when there is no wake phrase. `strict` (default: while the PC plays
    sound): only "Hey/OK Ari", not a bare "Ari" or a mishearing like "Hey Harry"."""
    strict = STRICT["on"] if strict is None else strict
    m = (WAKE_STRICT if strict else WAKE_ANY).match(text.strip())
    if not m:
        return None
    return text.strip()[m.end():].strip(" ,.!?")


def speak_hours_ok(hours: str, now: time.struct_time) -> bool:
    a, b = hours.split("-")
    m = now.tm_hour * 60 + now.tm_min
    lo, hi = (int(x[:2]) * 60 + int(x[3:]) for x in (a, b))
    return lo <= m < hi if lo <= hi else (m >= lo or m < hi)


def notice_text(title: str, text: str) -> str:
    """An important phone message, as one or two short spoken sentences."""
    first = re.split(r"(?<=[.!?])\s|\n", " ".join(text.split("\n")[:2]).strip())[0] if text.strip() else ""
    t = title.strip().rstrip(".")
    said = t if not first or first.lower().startswith(t.lower()) else f"{t}. {first}"
    return ("Heads up: " + (said if said else first)).strip()[:220]


# ------------------------------------------------------------------ talking to argusd

class AriClient:
    def here(self) -> None:
        """Tell Argus this listener runs (so the island's Talk button can use it), with the mic it hears and how
        loud the loudest sound was in the last minute (the health page warns about a quiet or wrong mic)."""
        try:
            self._req("POST", "/ari/listener", dict(MIC), timeout=5)
        except Exception:  # argusd restarting
            pass

    def wakes(self, after: int, kinds: str = "ari.wake", wait: float = 0) -> tuple[list[dict], int]:
        """Talk-button presses (and important notices) since event `after`: (events, the newest seq). `wait`: when
        there are none yet, Argus waits up to that long for one (a long poll)."""
        try:
            more = f"&wait={wait:g}" if wait and after >= 0 else ""
            r = json.loads(self._req("GET", f"/events?kinds={kinds}&after={max(after, 0)}&limit=20{more}", None,
                                     timeout=5 + wait))
        except Exception:
            return [], after
        if after < 0:  # the first look: only what comes from now on
            return [], int(r.get("seq") or 0)
        evs = r.get("events") or []
        return evs, max([after] + [int(e["seq"]) for e in evs])

    def __init__(self, url: str, token: str | None, conv_file: Path,
                 local: Callable[[str], bytes | None] | None = None):
        self.url, self.token, self.conv_file = url.rstrip("/"), token, conv_file
        self.local = local  # Ari's voice made on this PC (local_voice)
        try:
            self.conv = conv_file.read_text().strip() or None
            self.used = conv_file.stat().st_mtime  # when we last talked (touched on every message)
        except OSError:
            self.conv, self.used = None, time.time()

    def _req(self, method: str, path: str, body: dict | None = None, timeout: float = 30) -> bytes:
        req = urllib.request.Request(self.url + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json",
                                              **({"Authorization": f"Bearer {self.token}"} if self.token else {})})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()

    NEW_CHAT_S = 30 * 60
    WAIT_S = 300.0  # how long an answer is waited for (the PC may have to wake its model first)

    def say(self, text: str, on_partial: Callable[[str, str], None] | None = None) -> dict:
        """Ask Ari. `on_partial(text_so_far, mood)`: the finished sentences of a reply still being written (small
        talk streams), so they can be spoken before the rest is ready."""
        LAST_ASKED[0] = started = time.monotonic()
        if self.conv and time.time() - self.used > self.NEW_CHAT_S:
            self.conv = None  # a while since we last talked: a fresh chat, so old ones don't leak into this one
        self.used = time.time()
        try:
            self.conv_file.touch()
        except OSError:
            pass
        r = json.loads(self._req("POST", "/ari", {"text": text, **({"conv": self.conv} if self.conv else {})}))
        if r.get("conv") and r["conv"] != self.conv:
            self.conv = r["conv"]
            try:
                self.conv_file.parent.mkdir(parents=True, exist_ok=True)
                self.conv_file.write_text(self.conv)
            except OSError:
                pass
        if r.get("reply") is None and r.get("job_id"):  # a model answers: wait for the turn to fill in
            job, seen, start = r["job_id"], 0, time.monotonic()
            turn_id = r.get("turn")
            n = 0
            while time.monotonic() - start < self.WAIT_S:  # asked in the background: it may take a while
                time.sleep(0.15)
                n += 1
                if STOPPED_AT[0] > started:  # the island's Stop button: drop this answer
                    return {"reply": "", "stopped": True}
                if on_partial is not None:
                    try:
                        q = f"/events?kinds=plugin.ari.partial&job={job}&after={seen}&limit=20"
                        evs = json.loads(self._req("GET", q, None, timeout=3)).get("events") or []
                        for e in evs:
                            seen = max(seen, int(e.get("seq") or 0))
                            d = e.get("data") or {}
                            on_partial(str(d.get("text") or ""), str(d.get("mood") or "neutral"))
                    except Exception as e:  # noqa: BLE001 - no streaming then; the whole reply still comes
                        log.debug("no partial reply", extra={"error": str(e)[:120]})
                if n % 2 and on_partial is not None:
                    continue  # the turn itself every 0.3 s
                turns = json.loads(self._req("GET", f"/ari/{self.conv}"))["turns"]
                # this question's own turn (by id, else by job): never an older answer that happens to be last
                mine = next((t for t in turns if (turn_id and t.get("id") == turn_id) or t.get("job_id") == job), None)
                if mine is not None and mine.get("text"):
                    return {"reply": mine["text"], "pending": mine.get("pending")}
            return {"reply": "I couldn't get an answer to that one. It's in Helios if it turns up."}
        return r

    def ears(self) -> None:
        """Is Ari paused right now? (asked once at start; then ari.listening events say)."""
        try:
            d = json.loads(self._req("GET", "/ari/listening", None, timeout=5))
            EARS["until"] = 0.0 if d.get("listening", True) else float(d.get("until") or 1e13)
        except Exception:
            pass

    def vocabulary(self) -> None:
        """The names to expect and the usual mishearings (argus.vocab), into VOCAB. Best effort."""
        try:
            VOCAB.update(json.loads(self._req("GET", "/ari/vocabulary", None, timeout=5)))
        except Exception:  # argusd restarting: keep what we had
            pass

    def state(self, phase: str, text: str = "") -> None:
        """Tell Argus what Ari is doing here (the Ari pill in Helios and the popup follow it). Best effort."""
        try:
            self._req("POST", "/ari/state", {"phase": phase, "text": text[:300], "by": "pc"}, timeout=3)
        except (OSError, urllib.error.HTTPError):
            pass

    def voice(self, text: str) -> bytes | None:
        """Ari's voice for a text, or None (then it is shown, not spoken)."""
        if self.local is not None:  # made here, on the PC's GPU: asking argusd would reach the same voice
            return self.local(text)
        try:
            return self._req("POST", "/ari-voice/say", {"text": text}, timeout=60)
        except urllib.error.HTTPError:
            return None


def local_voice(cfg: Any) -> Callable[[str], bytes | None]:
    """Ari's voice made here, where you hear it: the expressive voice server on this PC's GPU. Otherwise every
    sentence goes to argusd and back, and after the move argusd is on the laptop, which has neither the GPU nor
    the voice server. If the voice can't speak, the answer is None and the words are shown instead."""
    from .voice import Expressive

    expr = Expressive(cfg.ari.expressive_url)
    threading.Thread(target=expr.warm, daemon=True, name="voice-warm").start()

    def say(text: str) -> bytes | None:
        wav = expr.say(text)
        if wav:
            return wav
        if expr.recent():  # it was speaking a moment ago: one more, slower try rather than giving up
            return expr.say(text, timeout=90, force=True)
        return None

    return say


# ------------------------------------------------------------------ audio in and out (sounddevice)

def mic_blocks(device=None, block: int = BLOCK) -> Iterator:
    import queue

    import sounddevice as sd  # type: ignore[import-not-found]

    q: queue.Queue = queue.Queue(maxsize=200)

    def cb(indata, frames, t, status):  # noqa: ARG001 - sounddevice's signature
        try:
            q.put_nowait((time.monotonic(), indata[:, 0].copy()))
        except queue.Full:
            pass

    with sd.InputStream(samplerate=RATE, channels=1, dtype="float32", blocksize=block, device=device, callback=cb):
        while True:
            yield q.get()


def idle_seconds() -> float:
    """How long since the last key press or mouse move on this PC (Windows); 0 elsewhere."""
    if os.name != "nt":
        return 0.0
    import ctypes

    class LastInput(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    li = LastInput()
    li.cbSize = ctypes.sizeof(li)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(li)):  # type: ignore[attr-defined]
        return 0.0
    return (ctypes.windll.kernel32.GetTickCount() - li.dwTime) / 1000.0  # type: ignore[attr-defined]


EARS: dict = {"until": 0.0}  # paused until then (Helios's pause button, the voice training page)


def paused() -> bool:
    return EARS["until"] > time.time()


def hush_from(evs: list[dict]) -> None:
    """Pause / resume from ari.listening events (the newest wins)."""
    for e in evs:
        if e.get("kind") == "ari.listening":
            d = e.get("data") or {}
            EARS["until"] = 0.0 if d.get("listening", True) else float(d.get("until") or 1e13)
            log.info("listening" if not paused() else "listening paused")


def ask_text(d: dict) -> str:
    """What Ari says when a task of its needs you ("Ari is stuck", "Ari needs something to go on", a yes / no)."""
    lines = [str(x) for x in d.get("summary") or []]
    title = str(d.get("title") or "")
    if title.startswith("Ari is stuck"):
        return "I'm stuck on that one. Have a look at the island and give me a hint?"
    if title.startswith("Ari needs") and len(lines) > 1:
        return f"Quick question: {lines[1].rstrip('?')}? It's on the island."
    if title.startswith("Ari wants to "):  # "Ari wants to press "Buy now"" -> "Can I press "Buy now"?"
        return f"Can I {title[len('Ari wants to '):]}? Yes or no on the island."
    return f"{title}. Okay to go ahead? It's on the island."


MIC: dict = {"mic": None, "loudest": None, "busy": None}  # the mic in use, its loudest level in the last minute,
# and whether you're in a call or a game (then Ari answers in text on the island, not aloud)
_BUSY_AT = [0.0]


def check_busy(client: AriClient, every: float = 10.0, look: Callable[[], Any] | None = None, cfg: Any = None) -> None:
    """In a call or a game? Looked at every `every` seconds; Argus hears at once when it changes. Off with
    ari.quiet_in_calls: false."""
    if time.monotonic() - _BUSY_AT[0] < every:
        return
    _BUSY_AT[0] = time.monotonic()
    from . import busy

    a = getattr(cfg, "ari", None)
    if a is not None and not getattr(a, "quiet_in_calls", True):
        got: Any = (None, "")
    else:
        got = (look or (lambda: busy.now(tuple(getattr(a, "call_apps", ()) or ()))))()
    now, app = got if isinstance(got, tuple) else (got, "")
    if now != MIC["busy"]:
        MIC["busy"] = now
        log.info("busy" if now else "not busy", extra={"with": now, "app": app})
        client.here()
VOCAB: dict = {"words": [], "heard_as": {}}  # filled from Argus (GET /ari/vocabulary), refreshed every 10 min


HALLUCINATIONS = re.compile(r"^(?:thank you(?: (?:so much|very much))?(?: for (?:watching|listening|joining us))?|"
                            r"thanks for watching|please subscribe|you)[.!]?$", re.I)


def made_up(seg: Any) -> bool:
    """A segment Whisper made up from noise: it thinks there was no speech and isn't sure of the words, or it is one
    of the phrases it says when it hears nothing ("Thank you for watching")."""
    nsp = float(getattr(seg, "no_speech_prob", 0.0) or 0.0)
    lp = float(getattr(seg, "avg_logprob", 0.0) or 0.0)
    text = str(getattr(seg, "text", "") or "").strip()
    return (nsp > 0.6 and lp < -0.8) or bool(HALLUCINATIONS.match(text) and (nsp > 0.3 or lp < -0.6))


def whisper(name: str, names: bool = True) -> Callable[[object], str]:
    """Whisper `name` as a function: audio -> what you said. `names`: biased towards the names Ari should expect
    (writing down a request); off for the wake check. A transcript that only reads those names back (Whisper on
    noise) comes back empty."""
    from . import vocab
    from .worker.hear import model

    m = model(name)
    lock = threading.Lock()

    def run(audio) -> str:
        words = list(VOCAB.get("words") or [])
        hot = vocab.hotwords(words) if names and words else None
        with lock:
            segs, _ = m.transcribe(audio, language="en", beam_size=1, vad_filter=False,  # type: ignore[attr-defined]
                                   condition_on_previous_text=False, without_timestamps=True, hotwords=hot)
            text = " ".join(s.text.strip() for s in segs if not made_up(s)).strip()
        if vocab.echo(text, words + list(vocab.BUILTIN)):
            log.info("heard only the names back (noise)", extra={"text": text[:80]})
            return ""
        return vocab.fix(text, VOCAB.get("heard_as") or {})

    return run


FOLLOW_MAX_WORDS = 25


def follow_ok(text: str) -> bool:
    """Something said without "Hey Ari" right after an answer: for Ari unless the PC is playing sound (the voices in
    a video or a call) or it is a long stream of words (the TV, someone else talking)."""
    return not STRICT["on"] and len(text.split()) <= FOLLOW_MAX_WORDS


def live(cfg, client: AriClient, wake_t: Callable, cmd_t: Callable, device) -> int:
    """Talking with Ari: "Hey Ari", then back and forth for a few seconds (voice_live.py says how)."""
    import numpy as np
    import sounddevice as sd  # type: ignore[import-not-found]

    from . import voice_live as vl

    models = cfg.db_path.parent / "models"
    sil = vl.silero_path()
    try:
        vad = vl.Quiet(vl.Silero(sil)) if sil else vl.LoudnessVad()  # Silero only when the room isn't silent
    except Exception as e:  # noqa: BLE001 - onnxruntime missing or broken: the loudness gate still works
        log.warning("no Silero VAD", extra={"error": str(e)[:200]})
        vad = vl.LoudnessVad()
    st_path = vl.ensure_smart_turn(models)
    finished = None
    if st_path:
        try:
            finished = vl.SmartTurn(st_path)
        except Exception as e:  # noqa: BLE001
            log.warning("no smart turn", extra={"error": str(e)[:200]})
    log.info("live conversation", extra={"vad": type(vad).__name__, "smart_turn": bool(finished)})

    def open_stream(rate: int, cb: Callable):
        s = sd.OutputStream(samplerate=rate, channels=1, dtype="float32", callback=cb, blocksize=0)
        s.start()
        return s

    def synth(text: str):
        wav = client.voice(text)
        if not wav:
            print(f"Ari: {text}", flush=True)
            return None
        return vl.wav_samples(wav)

    def synth_quiet(text: str):
        wav = client.voice(text)  # fillers: Ari's one voice
        return vl.wav_samples(wav) if wav else None

    player = vl.Player(synth, open_stream, report=client.state)
    gate = vl.EchoGate()

    fillers = vl.Fillers(synth_quiet)

    def wait_sound(chat: bool) -> None:
        """An answer taking a while: a short "hmm, let me check" (then the soft pulse), or just the pulse (small talk
        doesn't need "let me check")."""
        f = None if chat else fillers.pick()
        if f is not None:
            player.filler(*f)
        else:
            player.thinking(True)

    def ask(text: str, partial: Callable[[str, str], None] | None = None) -> dict:
        from .worker.think import chatty

        chat = chatty(text)
        t = threading.Timer(1.2 if chat else 0.8, wait_sound, args=(chat,))  # a task: "hmm, let me check" sooner
        t.start()

        def first_words(so_far: str, mood: str) -> None:  # Ari starts talking: no "let me check" now
            t.cancel()
            if partial is not None:
                partial(so_far, mood)

        try:
            return client.say(text, first_words)
        finally:
            t.cancel()
            if not player.busy:
                player.thinking(False)

    talk = vl.Talk(turns=vl.Turns(finished=finished), transcribe=cmd_t, transcribe_wake=wake_t, wake_rest=wake_rest,
                   ask=ask, player=player, report=client.state, chime=lambda: player.cue(vl.chime_samples(), RATE),
                   idle_s=float(cfg.ari.talk_idle_s), on_false_barge=gate.fooled,
                   live_words=wake_t if cfg.ari.live_words else None, show=client.state, ask_async=True,
                   quiet=lambda: MIC["busy"] is not None, follow_ok=follow_ok)
    spoke_up = [0.0]
    voc_at = [time.monotonic()]

    def watch() -> None:
        """The Talk button, important notices and pause / resume: a long poll, so Argus is asked about once every
        20 s while nothing happens, and a press still lands at once."""
        seq = -1
        while True:
            t0 = time.monotonic()
            evs, seq = client.wakes(seq, "ari.wake,ari.notice,ari.listening,ari.stop,ari.ask", wait=20)
            hush_from(evs)
            if any(e.get("kind") == "ari.stop" for e in evs):
                log.info("stop button")
                STOPPED_AT[0] = time.monotonic()
                player.stop()  # quiet at once; an answer still being written is dropped by say()
                talk.drop_pending()
            if any(e.get("kind") == "ari.wake" for e in evs):
                log.info("talk button")
                talk.wake()
            asks = [e for e in evs if e.get("kind") == "ari.ask"]
            if asks and not paused() and not MIC["busy"]:  # Ari's task needs you: said once, the box is on the island
                player.add(vl.sentences(ask_text(asks[-1].get("data") or {})))
            notes = [e for e in evs if e.get("kind") == "ari.notice"]
            if notes and cfg.ari.speak_up and not paused() and speak_hours_ok(cfg.ari.speak_hours, time.localtime()) \
                    and idle_seconds() < 300 and time.monotonic() - spoke_up[0] > 600 and not player.busy \
                    and not talk.in_conversation:
                d = notes[-1].get("data") or {}
                spoke_up[0] = time.monotonic()
                log.info("speaking up", extra={"title": d.get("title")})
                player.say(vl.sentences(notice_text(str(d.get("title") or ""), str(d.get("text") or ""))))
            if not evs and time.monotonic() - t0 < 1:  # Argus down, or one that doesn't wait: not a tight loop
                time.sleep(2)

    def side() -> None:
        """"I'm here" and the vocabulary now and then; the conversation's clock. Ticks often only while there is
        something to time (a conversation, Ari talking); otherwise twice a second."""
        last = 0.0
        while True:
            if time.monotonic() - last > 30:
                client.here()
                if time.monotonic() - voc_at[0] > 600:
                    client.vocabulary()
                    voc_at[0] = time.monotonic()
                last = time.monotonic()
            check_busy(client, cfg=cfg)
            player.poll()
            talk.tick()
            busy = player.busy or talk.in_conversation or talk.turns.talking
            time.sleep(0.1 if busy else 0.5)

    threading.Thread(target=watch, daemon=True, name="ari-watch").start()
    threading.Thread(target=side, daemon=True, name="ari-side").start()
    guard = vl.PlaybackGuard() if cfg.ari.playback_guard else None
    if guard is not None:
        threading.Thread(target=loopback, args=(guard,), daemon=True, name="ari-loopback").start()
    wake_word = WakeWord.load(cfg, talk)
    if wake_word is not None:  # the wake-word model decides "Hey Ari"; Whisper only writes down what follows
        talk.transcribe_wake = lambda audio: ""
    try:
        MIC["mic"] = str(sd.query_devices(device, "input").get("name"))
    except Exception:  # noqa: BLE001 - only for the log and the health page
        pass
    log.info("listening", extra={"mic": MIC["mic"], "playback_guard": guard is not None,
                                 "wake_model": wake_word is not None})
    client.state("idle", "")  # the island's "starting…" ends: Ari can hear now
    print("Listening for \"Hey Ari\" (Ctrl+C to stop). Then just talk; \"thanks Ari\" ends it.", flush=True)
    loudest, since = 0.0, time.monotonic()
    try:
        for at, frame in mic_blocks(device, vl.FRAME):
            if time.monotonic() - at > 1.0:  # heard while Ari was busy thinking: too old to act on
                continue
            rms = float(np.sqrt(np.mean(np.square(frame))))
            loudest = max(loudest, rms)
            if time.monotonic() - since > 60:
                skipped = vad.share_skipped() if isinstance(vad, vl.Quiet) else 0.0
                log.info("microphone level", extra={"loudest": round(loudest, 4), "vad_skipped": round(skipped, 2)})
                MIC["loudest"] = round(loudest, 4)
                loudest, since = 0.0, time.monotonic()
            if paused():  # the pause button / voice training: hear nothing, say nothing new
                if talk.turns.talking or talk.in_conversation:
                    talk.turns.reset()
                    talk.in_talk_until = 0.0
                continue
            voice = vad(frame)
            if not gate(frame, player.busy and not talk.paused_for_barge):
                voice = 0.0
            if guard is not None:
                STRICT["on"] = guard.active
                if guard.explains(rms):  # the PC's own sound coming back (a video, music, Ari): not you
                    voice = 0.0
            if wake_word is not None:
                wake_word.feed(frame)
            try:
                talk.frame(frame, voice)
            except Exception as e:  # noqa: BLE001 - never stop listening because of one turn
                log.warning("turn failed", extra={"error": str(e)[:200]})
                talk.turns.reset()
    except KeyboardInterrupt:
        pass
    return 0


def loopback(guard: Any, every_s: float = 60.0) -> None:
    """What the speakers play, for the PlaybackGuard (WASAPI loopback through the `soundcard` package). Follows the
    default speaker (headphones plugged in: a new one). Without the package: no guard, said once in the log."""
    import numpy as np

    try:
        import soundcard as sc  # type: ignore[import-not-found]
    except Exception as e:  # noqa: BLE001 - not installed (pip install -e .[listen]) or no WASAPI
        log.info("no playback guard (pip install -e .[listen])", extra={"error": str(e)[:120]})
        return
    from . import voice_live as vl

    while True:
        try:
            spk = sc.default_speaker()
            rec_dev = sc.get_microphone(id=str(spk.name), include_loopback=True)
            log.info("playback guard on", extra={"speaker": spk.name})
            opened = time.monotonic()
            with rec_dev.recorder(samplerate=RATE, channels=1, blocksize=vl.FRAME) as rec:
                while True:
                    data = rec.record(numframes=vl.FRAME)
                    guard.played(float(np.sqrt(np.mean(np.square(data)))) if len(data) else 0.0)
                    if time.monotonic() - opened > every_s:
                        if str(sc.default_speaker().name) != str(spk.name):
                            break  # another speaker now (headphones): listen to that one
                        opened = time.monotonic()
        except Exception as e:  # noqa: BLE001 - the device went away: try again shortly
            log.info("playback guard paused", extra={"error": str(e)[:120]})
            time.sleep(5)


class WakeWord:
    """A wake-word model for "Hey Ari" (openWakeWord, ari.wake_model): small, on the CPU, scores the sound itself
    every 80 ms, so a video saying "Ari" in a sentence or a mishearing doesn't wake Ari. When it fires, Ari listens
    (like the island's Talk button) and Whisper writes down what you say."""

    CHUNK = 1280  # 80 ms at 16 kHz: what openWakeWord scores at a time
    COOL_S = 2.0

    def __init__(self, model: Any, threshold: float, on_wake: Callable[[], None], clock: Callable[[], float] =
                 time.monotonic):
        import numpy as np

        self.model, self.threshold, self.on_wake, self.clock = model, threshold, on_wake, clock
        self._buf = np.zeros(0, dtype=np.float32)
        self._last = -1e9

    @classmethod
    def load(cls, cfg: Any, talk: Any) -> WakeWord | None:
        path = str(cfg.ari.wake_model or "").strip()
        if not path:
            return None
        p = Path(path).expanduser()
        p = p if p.is_absolute() else cfg.base_dir / p
        if not p.exists():
            log.warning("wake-word model not found: Whisper listens for \"Hey Ari\"", extra={"path": str(p)})
            return None
        try:
            from openwakeword.model import Model  # type: ignore[import-not-found]

            m = Model(wakeword_models=[str(p)], inference_framework="onnx")
        except Exception as e:  # noqa: BLE001 - not installed (pip install -e .[wake]) or a bad file
            log.warning("wake-word model not loaded: Whisper listens for \"Hey Ari\"", extra={"error": str(e)[:160]})
            return None
        log.info("wake-word model", extra={"path": str(p), "threshold": cfg.ari.wake_threshold})
        return cls(m, float(cfg.ari.wake_threshold), talk.wake)

    def feed(self, frame: Any) -> bool:
        """A mic frame (float32, 16 kHz). True when "Hey Ari" was just heard."""
        import numpy as np

        self._buf = np.concatenate([self._buf, np.asarray(frame, dtype=np.float32).reshape(-1)])
        fired = False
        while len(self._buf) >= self.CHUNK:
            chunk, self._buf = self._buf[: self.CHUNK], self._buf[self.CHUNK:]
            scores = self.model.predict((np.clip(chunk, -1, 1) * 32767).astype(np.int16))
            best = max((float(v) for v in (scores or {}).values()), default=0.0)
            threshold = self.threshold * (1.3 if STRICT["on"] else 1.0)  # stricter while the PC plays sound
            if best >= min(threshold, 0.95) and self.clock() - self._last > self.COOL_S:
                self._last = self.clock()
                log.info("wake word", extra={"score": round(best, 3)})
                self.on_wake()
                fired = True
        return fired


LOCK_PORT = 8619


STOPPED_AT = [0.0]  # when the island's Stop button was last pressed (monotonic)
LAST_ASKED = [0.0]  # when Ari was last asked something here (monotonic)


def keep_warm(cfg, every: float = 240.0, check: float = 15.0,
              away: Callable[[], float] | None = None) -> threading.Thread | None:
    """Ari's first local model stays loaded in Ollama while you're around (ari.keep_warm): loading it on your first
    question costs seconds. A tiny request now and every few minutes, so it is never unloaded. When you've been
    away (no keyboard or mouse, nothing asked) for ari.rest_after_min, it stops, and Ollama lets the model go
    (GPU memory and power back); the moment you're back it is loaded again, before you say anything."""
    t1 = cfg.models.tiers.get(cfg.models.chain[0]) if cfg.models.chain else None
    if not cfg.ari.keep_warm or t1 is None or t1.provider != "ollama" or not t1.model:
        return None
    import json
    import urllib.request

    body = json.dumps({"model": t1.model, "prompt": "", "keep_alive": "10m",  # the same context as Ari's requests
                       "options": {"num_ctx": cfg.ollama.num_ctx}}).encode()
    rest_s = float(cfg.ari.rest_after_min) * 60
    gone = away or (lambda: min(idle_seconds(), time.monotonic() - LAST_ASKED[0]))

    def loop() -> None:
        last, resting = -1e9, False
        while True:
            now_resting = rest_s > 0 and gone() > rest_s
            if now_resting and not resting:
                log.info("you're away: letting the model unload")
            if not now_resting and (resting or time.monotonic() - last >= every):
                try:
                    req = urllib.request.Request(cfg.ollama.url.rstrip("/") + "/api/generate", data=body,
                                                 method="POST", headers={"Content-Type": "application/json"})
                    urllib.request.urlopen(req, timeout=120).read()  # noqa: S310 - the local Ollama
                    if resting:
                        log.info("you're back: model loaded again")
                except Exception as e:  # noqa: BLE001 - Ollama down: try again later
                    log.info("could not warm the model", extra={"error": str(e)[:120]})
                last = time.monotonic()
            resting = now_resting
            time.sleep(check)

    th = threading.Thread(target=loop, daemon=True, name="keep-warm")
    th.start()
    log.info("keeping the model warm", extra={"model": t1.model, "rest_after_min": cfg.ari.rest_after_min})
    return th


def only_one():
    """Hold a local port for as long as this listener runs: a second ari-listen (a restart that overlapped, a second
    `dev.ps1 up`) can't take it, and quits instead of answering "Hey Ari" a second time. None when taken."""
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if os.name == "nt":
        s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)  # type: ignore[attr-defined]
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
        s.listen(1)
    except OSError:
        s.close()
        return None
    return s


def main(argv: list[str] | None = None) -> int:
    from .config import load_config, parse_env_file
    from .logs import setup_logging

    p = argparse.ArgumentParser(prog="ari-listen", description="Hey Ari on this PC's microphone")
    p.add_argument("--url", default=os.environ.get("ARGUS_URL", "http://127.0.0.1:8600"))
    p.add_argument("--device", default=None, help="microphone (name or number; `python -m sounddevice` lists them)")
    p.add_argument("--log-file", default="logs/ari.log")
    args = p.parse_args(argv)
    lock = only_one()
    if lock is None:
        print("ari-listen is already running on this PC (two would both answer).", file=sys.stderr)
        return 0
    setup_logging("INFO", Path(args.log_file))
    cfg = load_config()
    token = os.environ.get("ARGUS_WORKER_TOKEN") or parse_env_file(Path(".env")).get("ARGUS_WORKER_TOKEN")
    try:
        import numpy  # noqa: F401
        import sounddevice  # noqa: F401 # type: ignore[import-not-found]
    except (ImportError, OSError) as e:
        print(f"ari-listen needs its extras: pip install -e .[listen]  ({e})", file=sys.stderr)
        return 2
    client = AriClient(args.url, token, cfg.db_path.parent / "ari-listen.conv")
    client.local = local_voice(cfg)

    client.vocabulary()
    client.ears()
    keep_warm(cfg)
    log.info("loading Whisper", extra={"wake": cfg.ari.listen_wake_model, "command": cfg.ari.whisper_model,
                                       "names": len(VOCAB.get("words") or [])})
    client.state("starting", "Ari is starting…")  # the island says so until Ari can hear
    wake_t = whisper(cfg.ari.listen_wake_model, names=False)  # "is this Hey Ari?": not nudged towards "Ari"
    small_cmd = whisper(cfg.ari.listen_wake_model)
    heard_with = str(VOCAB.get("whisper_model") or cfg.ari.whisper_model)  # Helios > Settings (a trained model)
    cmd_t = small_cmd
    if heard_with != cfg.ari.listen_wake_model:
        # the bigger model loads in the background (~40 s): Ari listens at once, with the small one until then
        big: dict = {"t": None}

        def load_big() -> None:
            try:
                big["t"] = whisper(heard_with)
                log.info("command model ready", extra={"model": heard_with})
            except Exception as e:  # noqa: BLE001 - keep the small one
                log.warning("command model not loaded", extra={"model": heard_with, "error": str(e)[:160]})

        threading.Thread(target=load_big, daemon=True, name="whisper-load").start()

        def cmd_t(audio):  # type: ignore[misc]
            return (big["t"] or small_cmd)(audio)
    device = int(args.device) if args.device and args.device.isdigit() else args.device
    return live(cfg, client, wake_t, cmd_t, device)


if __name__ == "__main__":
    sys.exit(main())
