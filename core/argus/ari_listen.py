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


# ------------------------------------------------------------------ the conversation loop (no audio in here)

@dataclass
class Listener:
    transcribe_wake: Callable[[object], str]  # short clip -> text (small model)
    transcribe: Callable[[object], str]  # command clip -> text (better model)
    say: Callable[[str], dict]  # text -> Ari's answer {reply, pending, ...}
    speak: Callable[[str], None]
    chime: Callable[[], None] = lambda: None
    report: Callable[[str], None] = lambda phase: None  # what Ari is doing, for the Ari pill / the PC's popup
    wake_max_ms: int = 4000
    follow_s: float = 8.0
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
            return None
        if len(rest.replace(" ", "")) > 2:
            return self._send(rest)
        self.chime()
        self.report("listening")
        self.armed_until = self.clock() + self.follow_s
        return None

    def _send(self, text: str) -> str:
        log.info("heard", extra={"text": text})
        try:
            ans = self.say(text)
        except Exception as e:  # argusd down
            log.warning("could not reach Ari", extra={"error": str(e)})
            self.speak("Sorry, I can't reach Argus right now.")
            return text
        reply = ans.get("reply") or ""
        if reply:
            self.speak(reply)
        if ans.get("pending"):
            self.armed_until = self.clock() + self.follow_s  # "Shall I?": answer without the wake phrase
            self.report("listening")
        return text


# ------------------------------------------------------------------ talking to argusd

class AriClient:
    def __init__(self, url: str, token: str | None, conv_file: Path):
        self.url, self.token, self.conv_file = url.rstrip("/"), token, conv_file
        try:
            self.conv = conv_file.read_text().strip() or None
        except OSError:
            self.conv = None

    def _req(self, method: str, path: str, body: dict | None = None, timeout: float = 30) -> bytes:
        req = urllib.request.Request(self.url + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json",
                                              **({"Authorization": f"Bearer {self.token}"} if self.token else {})})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()

    def say(self, text: str) -> dict:
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

def mic_blocks(device=None) -> Iterator:
    import queue

    import sounddevice as sd  # type: ignore[import-not-found]

    q: queue.Queue = queue.Queue(maxsize=200)

    def cb(indata, frames, t, status):  # noqa: ARG001 - sounddevice's signature
        try:
            q.put_nowait((time.monotonic(), indata[:, 0].copy()))
        except queue.Full:
            pass

    with sd.InputStream(samplerate=RATE, channels=1, dtype="float32", blocksize=BLOCK, device=device, callback=cb):
        while True:
            yield q.get()


def play_wav(data: bytes) -> None:
    import numpy as np
    import sounddevice as sd  # type: ignore[import-not-found]

    with wave.open(io.BytesIO(data)) as w:
        frames = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
        sd.play(frames, w.getframerate(), blocking=True)


def chime() -> None:
    import numpy as np
    import sounddevice as sd  # type: ignore[import-not-found]

    t = np.linspace(0, 0.12, int(RATE * 0.12), endpoint=False)
    tone = np.concatenate([np.sin(2 * np.pi * 880 * t), np.sin(2 * np.pi * 1320 * t)]) * 0.2
    sd.play(tone.astype(np.float32), RATE, blocking=True)


def whisper(name: str) -> Callable[[object], str]:
    from .worker.hear import model

    m = model(name)
    lock = threading.Lock()

    def run(audio) -> str:
        with lock:
            segs, _ = m.transcribe(audio, language="en", beam_size=1, vad_filter=False,  # type: ignore[attr-defined]
                                   condition_on_previous_text=False, without_timestamps=True)
            return " ".join(s.text.strip() for s in segs).strip()

    return run


def main(argv: list[str] | None = None) -> int:
    from .config import load_config, parse_env_file
    from .logs import setup_logging

    p = argparse.ArgumentParser(prog="ari-listen", description="Hey Ari on this PC's microphone")
    p.add_argument("--url", default=os.environ.get("ARGUS_URL", "http://127.0.0.1:8600"))
    p.add_argument("--device", default=None, help="microphone (name or number; `python -m sounddevice` lists them)")
    p.add_argument("--log-file", default="logs/ari.log")
    args = p.parse_args(argv)
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

    quiet_until = [0.0]  # what the mic heard while Ari was speaking is Ari's own voice: dropped

    def speak(text: str) -> None:
        wav = client.voice(text)
        if wav:
            client.state("speaking", text)
            play_wav(wav)
            client.state("done", text)
        else:
            print(f"Ari: {text}", flush=True)
        quiet_until[0] = time.monotonic() + 0.3

    def ding() -> None:
        chime()
        quiet_until[0] = time.monotonic() + 0.2

    log.info("loading Whisper", extra={"wake": cfg.ari.listen_wake_model, "command": cfg.ari.whisper_model})
    wake_t = whisper(cfg.ari.listen_wake_model)
    cmd_t = whisper(cfg.ari.whisper_model) if cfg.ari.whisper_model != cfg.ari.listen_wake_model else wake_t
    listener = Listener(transcribe_wake=wake_t, transcribe=cmd_t, say=client.say, speak=speak, chime=ding,
                        report=client.state)
    seg = Segmenter()
    device = int(args.device) if args.device and args.device.isdigit() else args.device
    log.info("listening for Hey Ari", extra={"device": device})
    print("Listening for \"Hey Ari\" (Ctrl+C to stop).", flush=True)
    try:
        for at, block in mic_blocks(device):
            if at < quiet_until[0]:
                seg = Segmenter(floor=seg.floor)  # forget a half-heard clip too
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
