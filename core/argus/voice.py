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
        """WAV audio of `text` (one call at a time; loading the voice the first time takes a second)."""
        text = re.sub(r"\s+", " ", text).strip()[:1000]
        if not text:
            raise ValueError("nothing to say")
        with self._lock:
            v = self._load()
            buf = io.BytesIO()
            with wave.open(buf, "wb") as wf:
                if hasattr(v, "synthesize_wav"):  # piper-tts 1.3+
                    try:
                        from piper import SynthesisConfig  # type: ignore[import-not-found]

                        v.synthesize_wav(text, wf, syn_config=SynthesisConfig(length_scale=1.0 / self.speed))
                    except ImportError:
                        v.synthesize_wav(text, wf)
                else:  # older piper-tts
                    v.synthesize(text, wf)
            return buf.getvalue()
