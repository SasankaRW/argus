"""Ari's hearing: Whisper on the PC (faster-whisper; the GPU when it can, else the CPU).

Job ari.transcribe, queued by argusd for `POST /ari/hear`: the worker fetches the recording from argusd
(`ctx.shared`), turns it into text and returns it. The model stays loaded between jobs.
"""

from __future__ import annotations

import io
import logging
import threading

from .workflows import Context, PermanentError, workflow

log = logging.getLogger("argus.hear")
_models: dict[str, object] = {}
_lock = threading.Lock()


def model(name: str):
    with _lock:
        if name not in _models:
            try:
                from faster_whisper import WhisperModel  # type: ignore[import-not-found]
            except ImportError:
                raise PermanentError("faster-whisper is not installed on this PC (pip install -e .[hearing])") from None
            try:
                m = WhisperModel(name, device="cuda", compute_type="float16")
                _prove(m)  # loading works without CUDA's libraries; the first real use is what fails
                _models[name] = m
                log.info("whisper loaded", extra={"model": name, "device": "cuda"})
            except Exception as e:  # no CUDA 12 cuBLAS / cuDNN 9: the CPU is fine for short commands
                log.warning("whisper on the GPU failed; using the CPU", extra={"error": str(e)[:200]})
                _models[name] = WhisperModel(name, device="cpu", compute_type="int8")
        return _models[name]


def _prove(m) -> None:
    """Run the model once on a moment of silence, so missing GPU libraries show up now, not on your first word."""
    import numpy as np

    segments, _ = m.transcribe(np.zeros(8000, dtype=np.float32), language="en", beam_size=1)
    list(segments)


def transcribe(audio: bytes, name: str) -> str:
    segments, _info = model(name).transcribe(io.BytesIO(audio), language="en", beam_size=1, vad_filter=True,
                                             condition_on_previous_text=False)
    return " ".join(s.text.strip() for s in segments).strip()


@workflow("ari", "transcribe")  # argusd sets needs=["gpu"] on the job
def transcribe_job(ctx: Context):
    name = str(ctx.input.get("audio") or "")
    if not name or ctx.shared is None:
        raise PermanentError("no recording")
    audio = ctx.shared(name)
    text = transcribe(audio, str(ctx.input.get("model") or "small.en"))
    return {"text": text}
