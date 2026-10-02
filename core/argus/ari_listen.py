"""Ari on the PC: listens for "Hey Ari" on the microphone, without a browser and without sending sound anywhere.

    python -m argus.ari_listen            (dev.ps1 up starts it when ari.listen is on)

How it hears:
1. The microphone is read all the time (16 kHz), but only sound louder than the room's background becomes a clip
   (the background level is learned as it goes).
2. A short clip (under 4 s) is written down by a small Whisper model on the GPU (`ari.listen_wake_model`,
   tiny.en) and checked for the wake phrase ("Hey Ari", "OK Ari", ...). Nothing leaves the PC.
3. After the wake phrase: what you said in the same breath is the command; otherwise a chime, then the next thing
   you say (written down with `ari.whisper_model`).
4. The command goes to Ari (POST /ari, the same conversation as Helios); the answer is spoken with the Piper voice
   (`ari.voice`) or, without one, printed. When Ari asks "Shall I?", you can answer without the wake phrase.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import wave
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("argus.ari_listen")
RATE = 16000
BLOCK = 480  # 30 ms


# ------------------------------------------------------------------ cutting speech out of the stream

@dataclass
class Segmenter:
    """Turns 30 ms blocks of samples into clips of speech: louder than the background, ending after `quiet_ms`."""
    quiet_ms: int = 700
    max_ms: int = 12000
    min_ms: int = 250
    ratio: float = 3.0  # how much louder than the background counts as speech
    floor: float = 0.004  # background level (RMS), learned while nobody speaks
    _clip: list = field(default_factory=list)
    _loud_at: int = 0
    _t: int = 0
    _start: int = 0

    def feed(self, block) -> list | None:
        """One block in; a finished clip (list of blocks) out, or None."""
        import numpy as np

        self._t += BLOCK * 1000 // RATE
        rms = float(np.sqrt(np.mean(np.square(block)))) if len(block) else 0.0
        loud = rms > max(self.floor * self.ratio, 0.01)
        if not self._clip:
            if loud:
                self._clip, self._start, self._loud_at = [block], self._t, self._t
            else:
                self.floor = 0.98 * self.floor + 0.02 * rms  # follow the room slowly
            return None
        self._clip.append(block)
        if loud:
            self._loud_at = self._t
        if self._t - self._loud_at >= self.quiet_ms or self._t - self._start >= self.max_ms:
            clip, self._clip = self._clip, []
            speech_ms = self._loud_at - self._start
            return clip if speech_ms >= self.min_ms else None
        return None


STRONG = "ari|arie|arri|aree|ary|aari|argus"
WEAK = "harry|hari|hurry|siri|sorry|audi|ori|aria|ali|ally|arty|artie|lorry"  # what "Ari" is misheard as
WAKE_ANY = re.compile(rf"(?:^|\b)(?:(?:hey|hi|hay|ok|okay|a)[\s,.]*(?:{STRONG}|{WEAK})|(?:{STRONG}))\b[\s,.!?]*", re.I)


def wake_rest(text: str) -> str | None:
    """What follows the wake phrase, or None when there is no wake phrase."""
    m = WAKE_ANY.search(text.strip())
    if not m:
        return None
    return text.strip()[m.end():].strip(" ,.!?")


STOP = re.compile(r"^(?:(?:ok(?:ay)?|no|hey)[\s,.]+)?(?:stop|wait|enough|quiet|be quiet|shut up|cancel|"
                  r"never ?mind|hold on|pause)\b", re.I)


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", s.lower()).split())


def barge(heard: str, speaking: str) -> tuple[str, str] | None:
    """Something said while Ari talks: ("stop", "") to stop it, ("wake", rest) for "Hey Ari, …" (rest may be a new
    command), or None (Ari's own voice coming back through the mic, or other talk)."""
    h = _norm(heard)
    if not h or h in _norm(speaking):  # the mic hearing Ari's own words
        return None
    if STOP.match(heard.strip()):
        return ("stop", "")
    rest = wake_rest(heard)
    if rest is not None:
        return ("wake", rest)
    return None


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


# ------------------------------------------------------------------ the conversation loop (no audio in here)

@dataclass
class Listener:
    transcribe_wake: Callable[[object], str]  # short clip -> text (small model)
    transcribe: Callable[[object], str]  # command clip -> text (better model)
    say: Callable[[str], dict]  # text -> Ari's answer {reply, pending, ...}
    speak: Callable[[str], float | None]  # starts speaking; returns how long it takes (s)
    chime: Callable[[], None] = lambda: None
    report: Callable[[str], None] = lambda phase: None  # what Ari is doing, for the Ari pill / the PC's popup
    stop_speaking: Callable[[], None] = lambda: None
    wake_max_ms: int = 4000
    follow_s: float = 8.0
    follow_up: bool = True  # after any answer, the next few seconds need no "Hey Ari"
    follow_up_s: float = 6.0
    armed_until: float = 0.0  # a command without the wake phrase is taken until then
    clock: Callable[[], float] = time.monotonic

    def clip(self, blocks: list) -> str | None:
        """One clip of speech. Returns the command sent to Ari, or None."""
        import numpy as np

        audio = np.concatenate(blocks)
        ms = len(audio) * 1000 // RATE
        if self.clock() < self.armed_until:  # Ari just asked, or the wake phrase came alone: this is the command
            text = self.transcribe(audio).strip()
            self.armed_until = 0.0
            if not text:
                self.report("idle")
                return None
            rest = wake_rest(text)
            return self._send(rest if rest else text)
        if ms > self.wake_max_ms:
            return None  # long talk that didn't start with the wake phrase (a call, the TV): not for us
        text = self.transcribe_wake(audio)
        rest = wake_rest(text)
        if rest is None:
            if text.strip():
                log.info("heard, no wake phrase", extra={"text": text.strip()[:80]})
            return None
        if len(rest.replace(" ", "")) > 2:
            return self._send(rest)
        self.chime()
        self.report("listening")
        self.armed_until = self.clock() + self.follow_s
        return None

    def wake(self) -> None:
        """Listen now without the wake phrase (the island's Talk button): the next thing said is the command."""
        self.chime()
        self.report("listening")
        self.armed_until = self.clock() + self.follow_s

    def _send(self, text: str) -> str:
        log.info("heard", extra={"text": text})
        try:
            ans = self.say(text)
        except Exception as e:  # argusd down
            log.warning("could not reach Ari", extra={"error": str(e)})
            self.speak("Sorry, I can't reach Argus right now.")
            return text
        reply = ans.get("reply") or ""
        took = float(self.speak(reply) or 0.0) if reply else 0.0
        if ans.get("pending"):
            self.armed_until = self.clock() + took + self.follow_s  # "Shall I?": answer without the wake phrase
            self.report("listening")
        elif self.follow_up and reply:
            self.armed_until = self.clock() + took + self.follow_up_s  # "and tomorrow?" without "Hey Ari"
        return text

    def interrupt(self, blocks: list, speaking: str) -> str | None:
        """A clip heard while Ari talks: "stop" stops it; "Hey Ari, …" stops it and takes the new command.
        Returns what was acted on, or None."""
        import numpy as np

        audio = np.concatenate(blocks)
        if len(audio) * 1000 // RATE > self.wake_max_ms:
            return None
        hit = barge(self.transcribe_wake(audio), speaking)
        if hit is None:
            return None
        kind, rest = hit
        self.stop_speaking()
        log.info("interrupted", extra={"by": kind, "rest": rest})
        if kind == "wake" and len(rest.replace(" ", "")) > 2:
            self._send(rest)
            return rest
        self.chime()
        self.report("listening")
        self.armed_until = self.clock() + self.follow_s
        return kind


# ------------------------------------------------------------------ talking to argusd

class AriClient:
    def here(self) -> None:
        """Tell Argus this listener runs (so the island's Talk button can use it)."""
        try:
            self._req("POST", "/ari/listener", {}, timeout=5)
        except Exception:  # argusd restarting
            pass

    def wakes(self, after: int, kinds: str = "ari.wake") -> tuple[list[dict], int]:
        """Talk-button presses (and important notices) since event `after`: (events, the newest seq)."""
        try:
            r = json.loads(self._req("GET", f"/events?kinds={kinds}&after={max(after, 0)}&limit=20", None,
                                     timeout=5))
        except Exception:
            return [], after
        if after < 0:  # the first look: only what comes from now on
            return [], int(r.get("seq") or 0)
        evs = r.get("events") or []
        return evs, max([after] + [int(e["seq"]) for e in evs])

    def __init__(self, url: str, token: str | None, conv_file: Path):
        self.url, self.token, self.conv_file = url.rstrip("/"), token, conv_file
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

    def say(self, text: str) -> dict:
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
            for _ in range(120):
                time.sleep(0.5)
                turns = json.loads(self._req("GET", f"/ari/{self.conv}"))["turns"]
                last = turns[-1] if turns else {}
                if last.get("text"):
                    return {"reply": last["text"], "pending": last.get("pending")}
            return {"reply": "That's taking a while; the answer will be in Helios."}
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
        try:
            return self._req("POST", "/ari-voice/say", {"text": text}, timeout=60)
        except urllib.error.HTTPError:
            return None


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


def play_wav(data: bytes, blocking: bool = True) -> float:
    """Play a WAV; returns its length in seconds. Not blocking: it plays while the mic keeps listening (so you
    can interrupt), and sounddevice.stop() cuts it off."""
    import numpy as np
    import sounddevice as sd  # type: ignore[import-not-found]

    with wave.open(io.BytesIO(data)) as w:
        frames = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
        rate = w.getframerate()
    sd.play(frames, rate, blocking=blocking)
    return len(frames) / rate


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


def chime() -> None:
    import numpy as np
    import sounddevice as sd  # type: ignore[import-not-found]

    t = np.linspace(0, 0.12, int(RATE * 0.12), endpoint=False)
    tone = np.concatenate([np.sin(2 * np.pi * 880 * t), np.sin(2 * np.pi * 1320 * t)]) * 0.2
    sd.play(tone.astype(np.float32), RATE, blocking=True)


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


VOCAB: dict = {"prompt": "", "heard_as": {}}  # filled from Argus (GET /ari/vocabulary), refreshed every 10 min


def whisper(name: str) -> Callable[[object], str]:
    from . import vocab
    from .worker.hear import model

    m = model(name)
    lock = threading.Lock()

    def run(audio) -> str:
        p = VOCAB.get("prompt") or None
        with lock:
            segs, _ = m.transcribe(audio, language="en", beam_size=1, vad_filter=False,  # type: ignore[attr-defined]
                                   condition_on_previous_text=False, without_timestamps=True,
                                   initial_prompt=p, hotwords=p)
            text = " ".join(s.text.strip() for s in segs).strip()
        return vocab.fix(text, VOCAB.get("heard_as") or {})

    return run


def live(cfg, client: AriClient, wake_t: Callable, cmd_t: Callable, device) -> int:
    """Talking with Ari as a conversation (ari.live; voice_live.py says how)."""
    import numpy as np
    import sounddevice as sd  # type: ignore[import-not-found]

    from . import voice_live as vl

    models = cfg.db_path.parent / "models"
    sil = vl.silero_path()
    try:
        vad = vl.Silero(sil) if sil else vl.LoudnessVad()
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
        wav = client.voice(text)
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

    def ask(text: str) -> dict:
        from .worker.think import chatty

        t = threading.Timer(1.2, wait_sound, args=(chatty(text),))
        t.start()
        try:
            return client.say(text)
        finally:
            t.cancel()
            if not player.busy:
                player.thinking(False)

    talk = vl.Talk(turns=vl.Turns(finished=finished), transcribe=cmd_t, transcribe_wake=wake_t, wake_rest=wake_rest,
                   ask=ask, player=player, report=client.state, chime=lambda: player.cue(vl.chime_samples(), RATE),
                   idle_s=float(cfg.ari.talk_idle_s), on_false_barge=gate.fooled)
    spoke_up = [0.0]
    voc_at = [time.monotonic()]

    def side() -> None:
        """The Talk button, important notices (as in the classic mode) and the "done talking" report."""
        seq, last = -1, 0.0
        while True:
            if time.monotonic() - last > 30:
                client.here()
                if time.monotonic() - voc_at[0] > 600:
                    client.vocabulary()
                    voc_at[0] = time.monotonic()
                last = time.monotonic()
            evs, seq = client.wakes(seq, "ari.wake,ari.notice,ari.listening")
            hush_from(evs)
            if any(e.get("kind") == "ari.wake" for e in evs):
                log.info("talk button")
                talk.wake()
            notes = [e for e in evs if e.get("kind") == "ari.notice"]
            if notes and cfg.ari.speak_up and not paused() and speak_hours_ok(cfg.ari.speak_hours, time.localtime()) \
                    and idle_seconds() < 300 and time.monotonic() - spoke_up[0] > 600 and not player.busy \
                    and not talk.in_conversation:
                d = notes[-1].get("data") or {}
                spoke_up[0] = time.monotonic()
                log.info("speaking up", extra={"title": d.get("title")})
                player.say(vl.sentences(notice_text(str(d.get("title") or ""), str(d.get("text") or ""))))
            for _ in range(8):
                player.poll()
                talk.tick()
                time.sleep(0.1)

    threading.Thread(target=side, daemon=True, name="ari-side").start()
    print("Listening for \"Hey Ari\" (Ctrl+C to stop). Then just talk; \"thanks Ari\" ends it.", flush=True)
    loudest, since = 0.0, time.monotonic()
    try:
        for at, frame in mic_blocks(device, vl.FRAME):
            if time.monotonic() - at > 1.0:  # heard while Ari was busy thinking: too old to act on
                continue
            loudest = max(loudest, float(np.sqrt(np.mean(np.square(frame)))))
            if time.monotonic() - since > 60:
                log.info("microphone level", extra={"loudest": round(loudest, 4)})
                loudest, since = 0.0, time.monotonic()
            if paused():  # the pause button / voice training: hear nothing, say nothing new
                if talk.turns.talking or talk.in_conversation:
                    talk.turns.reset()
                    talk.in_talk_until = 0.0
                continue
            voice = vad(frame)
            if not gate(frame, player.busy and not talk.paused_for_barge):
                voice = 0.0
            try:
                talk.frame(frame, voice)
            except Exception as e:  # noqa: BLE001 - never stop listening because of one turn
                log.warning("turn failed", extra={"error": str(e)[:200]})
                talk.turns.reset()
    except KeyboardInterrupt:
        pass
    return 0


LOCK_PORT = 8619


def keep_warm(cfg, every: float = 240.0) -> threading.Thread | None:
    """Ari's first local model stays loaded in Ollama while this listener runs (ari.keep_warm): loading it on your
    first question costs seconds. A tiny request now and every few minutes, so it is never unloaded."""
    t1 = cfg.models.tiers.get(cfg.models.chain[0]) if cfg.models.chain else None
    if not cfg.ari.keep_warm or t1 is None or t1.provider != "ollama" or not t1.model:
        return None
    import json
    import urllib.request

    body = json.dumps({"model": t1.model, "prompt": "", "keep_alive": "10m"}).encode()

    def loop() -> None:
        while True:
            try:
                req = urllib.request.Request(cfg.ollama.url.rstrip("/") + "/api/generate", data=body, method="POST",
                                             headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=120).read()  # noqa: S310 - the local Ollama
            except Exception as e:  # noqa: BLE001 - Ollama down: try again later
                log.info("could not warm the model", extra={"error": str(e)[:120]})
            time.sleep(every)

    th = threading.Thread(target=loop, daemon=True, name="keep-warm")
    th.start()
    log.info("keeping the model warm", extra={"model": t1.model})
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

    quiet_until = [0.0]  # just after Ari stops talking, the room's echo is dropped
    talking = {"until": 0.0, "text": "", "n": 0}  # while Ari speaks: only "stop" / "Hey Ari" are listened for
    out_lock = threading.Lock()

    def speak(text: str) -> float:
        wav = client.voice(text)
        if not wav:
            print(f"Ari: {text}", flush=True)
            return 0.0
        with out_lock:
            client.state("speaking", text)
            took = play_wav(wav, blocking=False)
            talking.update(until=time.monotonic() + took, text=text, n=talking["n"] + 1)
            n = talking["n"]
        quiet_until[0] = time.monotonic() + took + 0.3

        def finished() -> None:
            time.sleep(took)
            if talking["n"] == n and talking["until"]:
                talking["until"] = 0.0
                client.state("done", text)

        threading.Thread(target=finished, daemon=True).start()
        return took

    def stop_speaking() -> None:
        import sounddevice as sd  # type: ignore[import-not-found]

        with out_lock:
            sd.stop()
            talking["until"] = 0.0
            quiet_until[0] = time.monotonic() + 0.2
        client.state("done", talking["text"])

    def ding() -> None:
        chime()
        quiet_until[0] = time.monotonic() + 0.2

    client.vocabulary()
    client.ears()
    keep_warm(cfg)
    log.info("loading Whisper", extra={"wake": cfg.ari.listen_wake_model, "command": cfg.ari.whisper_model,
                                       "expects": VOCAB.get("prompt", "")[:120]})
    wake_t = whisper(cfg.ari.listen_wake_model)
    heard_with = str(VOCAB.get("whisper_model") or cfg.ari.whisper_model)  # Helios > Settings (a trained model)
    cmd_t = whisper(heard_with) if heard_with != cfg.ari.listen_wake_model else wake_t
    device = int(args.device) if args.device and args.device.isdigit() else args.device
    if cfg.ari.live:
        return live(cfg, client, wake_t, cmd_t, device)
    listener = Listener(transcribe_wake=wake_t, transcribe=cmd_t, say=client.say, speak=speak, chime=ding,
                        report=client.state, stop_speaking=stop_speaking, follow_up=cfg.ari.follow_up)
    seg = Segmenter()
    echo_seg = Segmenter(ratio=5.0)  # while Ari talks: only clearly louder speech (you, not the speakers)
    spoke_up = [0.0]

    def talk_button() -> None:
        """The island's Talk button (event ari.wake): listen now; important notices (ari.notice): say them when
        you're at the PC, within ari.speak_hours, at most one every 10 minutes. Tells Argus this listener runs."""
        seq, last = -1, 0.0
        while True:
            if time.monotonic() - last > 30:
                client.here()
                last = time.monotonic()
            evs, seq = client.wakes(seq, "ari.wake,ari.notice,ari.listening")
            hush_from(evs)
            if any(e.get("kind") == "ari.wake" for e in evs):
                log.info("talk button")
                listener.wake()
                quiet_until[0] = time.monotonic() + 0.2
            notes = [e for e in evs if e.get("kind") == "ari.notice"]
            if notes and cfg.ari.speak_up and not paused() and speak_hours_ok(cfg.ari.speak_hours, time.localtime()) \
                    and idle_seconds() < 300 and time.monotonic() - spoke_up[0] > 600 and not talking["until"]:
                d = notes[-1].get("data") or {}
                spoke_up[0] = time.monotonic()
                log.info("speaking up", extra={"title": d.get("title")})
                speak(notice_text(str(d.get("title") or ""), str(d.get("text") or "")))
            time.sleep(0.8)

    threading.Thread(target=talk_button, daemon=True, name="talk-button").start()
    log.info("listening for Hey Ari", extra={"device": device})
    print("Listening for \"Hey Ari\" (Ctrl+C to stop).", flush=True)
    try:
        import sounddevice as sd  # type: ignore[import-not-found]

        dev = sd.query_devices(device, "input")
        log.info("microphone", extra={"mic": dev.get("name"), "device": device})
    except Exception as e:  # noqa: BLE001 - only for the log
        log.warning("no microphone found", extra={"error": str(e)[:200]})
    loudest, since = 0.0, time.monotonic()
    try:
        for at, block in mic_blocks(device):
            import numpy as np

            loudest = max(loudest, float(np.sqrt(np.mean(np.square(block)))) if len(block) else 0.0)
            if time.monotonic() - since > 60:  # once a minute: is the microphone hearing anything at all?
                log.info("microphone level", extra={"loudest": round(loudest, 4), "background": round(seg.floor, 4)})
                loudest, since = 0.0, time.monotonic()
            if paused():
                seg = Segmenter(floor=seg.floor)
                continue
            if talking["until"] and at < talking["until"]:  # Ari is talking: listen only for "stop" / "Hey Ari"
                seg = Segmenter(floor=seg.floor)
                c = echo_seg.feed(block)
                if c is not None:
                    try:
                        listener.interrupt(c, talking["text"])
                    except Exception as e:  # noqa: BLE001 - keep listening
                        log.warning("interrupt failed", extra={"error": str(e)})
                continue
            if at < quiet_until[0]:
                seg = Segmenter(floor=seg.floor)  # forget a half-heard clip too
                echo_seg = Segmenter(floor=echo_seg.floor, ratio=5.0)
                continue
            clip = seg.feed(block)
            if clip is not None:
                try:
                    listener.clip(clip)
                except Exception as e:  # never stop listening because of one clip
                    log.warning("clip failed", extra={"error": str(e)})
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
