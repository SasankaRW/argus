"""Ari's natural voice: Piper text-to-speech, where argusd runs (fast enough on a laptop CPU).

`ari.voice` in argus.yaml names a Piper voice file (.onnx, its .onnx.json beside it). Without one (or without the
piper-tts package), Helios uses the browser's own voice.
"""

from __future__ import annotations

import io
import logging
import re
import threading
import wave
from pathlib import Path

log = logging.getLogger("argus.voice")


class VoiceUnavailable(Exception):
    pass


# Voices you can pick in Helios (Piper's English voices; the first time one is picked it is downloaded, ~60 MB).
CATALOG: list[tuple[str, str]] = [
    ("en_US-lessac-medium", "Lessac · US · warm, clear"),
    ("en_US-amy-medium", "Amy · US · female"),
    ("en_US-kristin-medium", "Kristin · US · female"),
    ("en_US-hfc_female-medium", "Hazel · US · female"),
    ("en_US-ryan-high", "Ryan · US · male, high quality"),
    ("en_US-joe-medium", "Joe · US · male"),
    ("en_US-hfc_male-medium", "Hugo · US · male"),
    ("en_GB-jenny_dioco-medium", "Jenny · UK · female"),
    ("en_GB-cori-high", "Cori · UK · female, high quality"),
    ("en_GB-alan-medium", "Alan · UK · male"),
    ("en_GB-northern_english_male-medium", "Northern · UK · male"),
]


def installed(folder: Path) -> list[str]:
    if not folder.is_dir():
        return []
    return sorted(p.stem for p in folder.glob("*.onnx") if p.with_suffix(".onnx.json").exists())


def download(name: str, folder: Path) -> Path:
    """Fetch a Piper voice into `folder` (its .onnx and .onnx.json). Raises VoiceUnavailable."""
    if not re.fullmatch(r"[a-z]{2}_[A-Z]{2}-[A-Za-z0-9_]+-(x_low|low|medium|high)", name):
        raise VoiceUnavailable(f"not a voice name: {name}")
    try:
        from piper.download_voices import download_voice  # type: ignore[import-not-found]
    except ImportError:
        raise VoiceUnavailable("piper-tts is not installed (pip install -e .[voice])") from None
    folder.mkdir(parents=True, exist_ok=True)
    try:
        download_voice(name, folder)
    except Exception as e:  # network, unknown voice
        raise VoiceUnavailable(f"could not download {name}: {e}") from None
    path = folder / f"{name}.onnx"
    if not path.exists():
        raise VoiceUnavailable(f"could not download {name}")
    return path


class Voice:
    def __init__(self, path: str, base: Path):
        p = Path(path).expanduser() if path else None
        self.path = (p if p is None or p.is_absolute() else base / p)
        self._voice = None
        self._lock = threading.Lock()
        self.speed = 1.0  # >1 faster, <1 slower

    def use(self, path: Path, speed: float | None = None) -> None:
        """Switch to another voice file (and speed)."""
        with self._lock:
            if path != self.path:
                self.path, self._voice = path, None
            if speed is not None:
                self.speed = max(0.6, min(1.6, speed))

    @property
    def name(self) -> str | None:
        return self.path.stem if self.path is not None else None

    @property
    def configured(self) -> bool:
        return self.path is not None

    def _load(self):
        if self._voice is None:
            if self.path is None:
                raise VoiceUnavailable("no voice set (ari.voice in argus.yaml)")
            if not self.path.exists():
                raise VoiceUnavailable(f"voice file not found: {self.path}")
            try:
                from piper import PiperVoice  # type: ignore[import-not-found]
            except ImportError:
                raise VoiceUnavailable("piper-tts is not installed (pip install -e .[voice])") from None
            self._voice = PiperVoice.load(str(self.path))
            log.info("voice loaded", extra={"voice": self.path.name})
        return self._voice

    def say(self, text: str) -> bytes:
        """WAV audio of `text` (one call at a time; loading the voice the first time takes a second). A mood tag
        ("[calm] ...") nudges the speed; [laugh] and the like are left out (Piper can't)."""
        from .expressive import PIPER_SPEED, mood_of, plain

        mood, _ = mood_of(text)
        nudge = PIPER_SPEED.get(mood, 1.0)
        text = re.sub(r"\s+", " ", plain(text)).strip()[:1000]
        if not text:
            raise ValueError("nothing to say")
        with self._lock:
            v = self._load()
            buf = io.BytesIO()
            with wave.open(buf, "wb") as wf:
                if hasattr(v, "synthesize_wav"):  # piper-tts 1.3+
                    try:
                        from piper import SynthesisConfig  # type: ignore[import-not-found]

                        v.synthesize_wav(text, wf,
                                         syn_config=SynthesisConfig(length_scale=1.0 / (self.speed * nudge)))
                    except ImportError:
                        v.synthesize_wav(text, wf)
                else:  # older piper-tts
                    v.synthesize(text, wf)
            return buf.getvalue()


CLIP_TEXT = ("Hi, I'm Ari. I keep an eye on things around here, and I'm always happy to help. "
             "Ask me anything, whenever you like. Honestly, it's nice to have someone to talk to.")


class Expressive:
    """The expressive voice server (voice_server.py) as argusd sees it: a WAV for a text, or None when it can't
    (not running, still loading, failed), and argusd then uses Piper."""

    def __init__(self, url: str, clip: Path | None, piper: Voice, timeout: float = 30):
        self.url, self.clip, self.piper, self.timeout = url.rstrip("/"), clip, piper, timeout
        self._down_until = 0.0

    def ensure_clip(self) -> Path | None:
        """The voice to sound like: ari.voice_clip, else made once from the Piper voice (so Ari keeps its voice)."""
        if self.clip is None or self.clip.exists():
            return self.clip
        try:
            self.clip.parent.mkdir(parents=True, exist_ok=True)
            self.clip.write_bytes(self.piper.say(CLIP_TEXT))
            log.info("voice clip made from the Piper voice", extra={"clip": str(self.clip)})
            return self.clip
        except Exception as e:  # noqa: BLE001 - no Piper voice: Chatterbox's own voice
            log.info("no voice clip", extra={"error": str(e)[:200]})
            return None

    def warm(self, tries: int = 24, wait: float = 5.0) -> bool:
        """Say one short word to the server (waiting for it to come up): its model, the voice to sound like and the
        GPU are ready before the first real sentence, which would otherwise pay for all that."""
        import json
        import time
        import urllib.request

        clip = self.ensure_clip()
        body = json.dumps({"text": "Hi.", "clip": str(clip.resolve()) if clip else ""}).encode()
        for i in range(tries):
            try:
                req = urllib.request.Request(self.url + "/say", data=body, method="POST",
                                             headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=120) as r:  # noqa: S310 - our own local service
                    r.read()
                log.info("expressive voice warmed up")
                return True
            except Exception as e:  # noqa: BLE001 - not up yet
                if i == tries - 1:
                    log.info("expressive voice not warmed up", extra={"error": str(e)[:120]})
                time.sleep(wait)
        return False

    def say(self, text: str) -> bytes | None:
        import json
        import time
        import urllib.request

        if time.time() < self._down_until:
            return None
        clip = self.ensure_clip()
        body = json.dumps({"text": text, "clip": str(clip.resolve()) if clip else ""}).encode()
        req = urllib.request.Request(self.url + "/say", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:  # noqa: S310 - our own local service
                return r.read()
        except Exception as e:  # noqa: BLE001 - down or failed: Piper for the next 60 s, then try again
            self._down_until = time.time() + 60
            log.warning("expressive voice unavailable, using Piper", extra={"error": str(e)[:200]})
            return None
