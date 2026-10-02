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
_gpu = {"ok": True}  # once the GPU fails, later models go straight to the CPU (a second try can hang forever)


def cuda_dlls() -> list[str]:
    """Windows: CTranslate2 (faster-whisper) needs CUDA 12's cuBLAS and cuDNN 9 DLLs. PyTorch's CUDA build and the
    nvidia-* pip packages carry them; make them findable (no separate CUDA install needed). Returns the folders."""
    import os
    import sys
    from importlib.util import find_spec

    if os.name != "nt" or not hasattr(os, "add_dll_directory"):
        return []
    dirs = []
    spec = find_spec("torch")
    if spec and spec.origin:
        dirs.append(os.path.join(os.path.dirname(spec.origin), "lib"))
    for sp in sys.path:
        nv = os.path.join(sp, "nvidia")
        if os.path.isdir(nv):
            dirs += [os.path.join(nv, d, "bin") for d in os.listdir(nv)]
    found = []
    for d in dirs:
        if os.path.isdir(d) and d not in _dll_dirs:
            try:
                os.add_dll_directory(d)
                os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
                _dll_dirs.add(d)
                found.append(d)
            except OSError:
                pass
    return found


_dll_dirs: set[str] = set()


def model(name: str):
    with _lock:
        if name not in _models:
            cuda_dlls()
            try:
                from faster_whisper import WhisperModel  # type: ignore[import-not-found]
            except ImportError:
                raise PermanentError("faster-whisper is not installed on this PC (pip install -e .[hearing])") from None
            if _gpu["ok"]:
                try:
                    m = _load(WhisperModel, name, "cuda", "float16")
                    _prove(m)  # loading works without CUDA's libraries; the first real use is what fails
                    _models[name] = m
                    log.info("whisper loaded", extra={"model": name, "device": "cuda"})
                except Exception as e:  # no CUDA 12 cuBLAS / cuDNN 9: the CPU is fine for short commands
                    _gpu["ok"] = False
                    log.warning("whisper on the GPU failed; using the CPU (slower). Fix: pip install -e .[hearing] "
                                "(adds CUDA 12 cuBLAS and cuDNN 9)", extra={"error": str(e)[:200]})
            if name not in _models:
                _models[name] = _load(WhisperModel, name, "cpu", "int8")
                log.info("whisper loaded", extra={"model": name, "device": "cpu"})
        return _models[name]


def _load(cls, name: str, device: str, compute: str):
    """The model from the local cache, with no call to huggingface.co (a second or more, and it fails offline);
    downloaded only the first time."""
    try:
        return cls(name, device=device, compute_type=compute, local_files_only=True)
    except Exception as e:  # noqa: BLE001 - not downloaded yet (or a GPU problem, which the caller handles)
        if "cuda" in str(e).lower() or "cublas" in str(e).lower() or "cudnn" in str(e).lower():
            raise
        return cls(name, device=device, compute_type=compute)


def _prove(m, limit: float = 30.0) -> None:
    """Run the model once on a moment of silence, so missing GPU libraries show up now, not on your first word.
    Raises when it fails or takes longer than `limit` seconds (a broken CUDA setup can hang instead of failing)."""
    import numpy as np

    out: dict[str, object] = {}

    def run() -> None:
        try:
            segments, _ = m.transcribe(np.zeros(8000, dtype=np.float32), language="en", beam_size=1)
            list(segments)
            out["ok"] = True
        except Exception as e:  # noqa: BLE001 - handed to the caller
            out["error"] = e

    t = threading.Thread(target=run, daemon=True, name="whisper-check")
    t.start()
    t.join(limit)
    if "error" in out:
        raise out["error"]  # type: ignore[misc]
    if not out.get("ok"):
        raise TimeoutError(f"the GPU did not answer in {limit:.0f} s")


def decode(audio: bytes):
    """A recording from the browser (webm/ogg/wav) as 16 kHz mono float samples. Done here rather than by
    faster-whisper, whose own decoder breaks with newer PyAV ("unexpected keyword argument 'metadata_errors'")."""
    import av  # type: ignore[import-not-found]  # comes with faster-whisper
    import numpy as np

    chunks = []
    with av.open(io.BytesIO(audio)) as c:
        rs = av.AudioResampler(format="s16", layout="mono", rate=16000)
        for frame in c.decode(audio=0):
            chunks += [f.to_ndarray() for f in rs.resample(frame)]
        chunks += [f.to_ndarray() for f in rs.resample(None)]
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate([c.reshape(-1) for c in chunks]).astype(np.float32) / 32768.0


def transcribe(audio: bytes, name: str, prompt: str = "") -> str:
    """Speech to text. `prompt` names the words to expect (argus.vocab), which steers Whisper's spelling."""
    segments, _info = model(name).transcribe(decode(audio), language="en", beam_size=1, vad_filter=True,
                                             condition_on_previous_text=False, initial_prompt=prompt or None,
                                             hotwords=prompt or None)
    return " ".join(s.text.strip() for s in segments).strip()


@workflow("ari", "transcribe")  # argusd sets needs=["gpu"] on the job
def transcribe_job(ctx: Context):
    name = str(ctx.input.get("audio") or "")
    if not name or ctx.shared is None:
        raise PermanentError("no recording")
    audio = ctx.shared(name)
    text = transcribe(audio, str(ctx.input.get("model") or "small.en"), str(ctx.input.get("prompt") or ""))
    return {"text": text}
