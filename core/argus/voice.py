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


class Voice:
    def __init__(self, path: str, base: Path):
        p = Path(path).expanduser() if path else None
        self.path = (p if p is None or p.is_absolute() else base / p)
        self._voice = None
        self._lock = threading.Lock()

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
        """WAV audio of `text` (one call at a time; loading the voice the first time takes a second)."""
        text = re.sub(r"\s+", " ", text).strip()[:1000]
        if not text:
            raise ValueError("nothing to say")
        with self._lock:
            v = self._load()
            buf = io.BytesIO()
            with wave.open(buf, "wb") as wf:
                if hasattr(v, "synthesize_wav"):  # piper-tts 1.3+
                    v.synthesize_wav(text, wf)
                else:  # older piper-tts
                    v.synthesize(text, wf)
            return buf.getvalue()
