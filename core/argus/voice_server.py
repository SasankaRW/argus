"""Ari's expressive voice: Chatterbox (Resemble AI, MIT) on the PC's GPU, as a tiny local web service.

It runs in its own Python environment (Chatterbox pins its own PyTorch, NumPy and Transformers), so argusd talks to
it over HTTP. The supervisor starts it when ari.voice_engine is "expressive" (docs/setup.md has the setup):

    .venv-voice\\Scripts\\python core\\argus\\voice_server.py        (listens on 127.0.0.1:8611)

POST /say {"text": "[cheerful] Oh nice! [laugh] ...", "clip": "data/voices/ari-clip.wav"} -> WAV
  The mood sets how lively it sounds, [laugh] / [sigh] / [chuckle] / [gasp] / [groan] / [clear throat] are real
  sounds. "clip" (optional, 5+ seconds) is the voice to sound like; without it, Chatterbox's own voice.
GET /health -> {"ok": true, "model": "turbo", "device": "cuda"}

Every clip it makes carries Resemble's inaudible watermark (Perth), as Chatterbox always adds one.
"""

from __future__ import annotations

import argparse
import inspect
import io
import json
import logging
import sys
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # core/, for argus.expressive (plain Python)
from argus.expressive import STYLE, for_voice, phrases  # noqa: E402

log = logging.getLogger("argus.voice_server")


class Engine:
    """Chatterbox, loaded once; one sentence at a time."""

    def __init__(self, model: str = "turbo", device: str | None = None):
        self.kind, self.device = model, device
        self.m = None
        self.lock = threading.Lock()
        self._loading = threading.Lock()
        self.clip: str | None = None

    def load(self):
        with self._loading:  # the start-up warm-up and the first sentence both ask: load the model once, not twice
            return self._load()

    def _load(self):
        if self.m is None:
            import torch

            dev = self.device or ("cuda" if torch.cuda.is_available() else "cpu")
            if self.kind == "turbo":
                from chatterbox.tts_turbo import ChatterboxTurboTTS  # type: ignore[import-not-found]

                self.m = ChatterboxTurboTTS.from_pretrained(device=dev)
            else:
                from chatterbox.tts import ChatterboxTTS  # type: ignore[import-not-found]

                self.m = ChatterboxTTS.from_pretrained(device=dev)
            self.device = dev
            log.info("chatterbox %s loaded on %s", self.kind, dev)
        return self.m

    def say(self, text: str, clip: str | None = None) -> bytes:
        """The whole reply, each phrase in its own mood ("[excited] We won! [sympathetic] Shame about the rain."),
        joined with a short breath between moods."""
        import numpy as np

        parts = [for_voice(f"[{m}] {t}") for m, t in phrases(text)]
        parts = [(m, w) for m, w in parts if w]
        if not parts:
            raise ValueError("nothing to say")
        with self.lock:
            m = self.load()
            if clip and clip != self.clip and Path(clip).is_file():
                m.prepare_conditionals(clip, exaggeration=STYLE["neutral"][0])  # the voice to sound like (once)
                self.clip = clip
            rate = int(m.sr)
            gap = np.zeros(int(0.12 * rate), dtype=np.float32)
            audio = []
            for i, (mood, words) in enumerate(parts):
                if i:
                    audio.append(gap)
                audio.append(self._one(m, mood, words))
        return to_wav(np.concatenate(audio), rate)

    def _one(self, m, mood: str, words: str):
        import numpy as np
        import torch

        lively, pace, temp = STYLE.get(mood, STYLE["neutral"])
        conds = getattr(m, "conds", None)
        if conds is not None and hasattr(conds, "t3"):  # how lively this phrase is
            conds.t3.emotion_adv = lively * torch.ones(1, 1, 1, device=getattr(m, "device", "cpu"))
        params = inspect.signature(m.generate).parameters
        kw = {"temperature": temp}
        if params.get("exaggeration") is not None and params["exaggeration"].default:  # the full model
            kw.update(exaggeration=lively, cfg_weight=pace)
        t0 = time.perf_counter()
        with torch.inference_mode():
            wav = m.generate(words, **{k: v for k, v in kw.items() if k in params})
        a = np.asarray(wav.squeeze(0).float().cpu().numpy() if hasattr(wav, "cpu") else wav, dtype=np.float32)
        log.info("said [%s] %d chars in %d ms", mood, len(words), int((time.perf_counter() - t0) * 1000))
        return a.reshape(-1)


def to_wav(a, rate: int) -> bytes:
    import numpy as np

    pcm = (np.clip(a, -1, 1) * 32767).astype(np.int16).tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def handler(engine) -> type[BaseHTTPRequestHandler]:
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet; the log line is in Engine.say
            pass

        def _send(self, code: int, body: bytes, kind: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            if self.path == "/health":
                self._send(200, json.dumps({"ok": True, "model": engine.kind, "device": engine.device,
                                            "loaded": engine.m is not None}).encode())
            else:
                self._send(404, b'{"error": "not found"}')

        def do_POST(self):  # noqa: N802
            if self.path != "/say":
                return self._send(404, b'{"error": "not found"}')
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(min(n, 20000)) or b"{}")
                text = str(body.get("text") or "")[:1000]
                wav = engine.say(text, str(body.get("clip") or "") or None)
            except ValueError as e:
                return self._send(422, json.dumps({"error": str(e)}).encode())
            except Exception as e:  # noqa: BLE001 - argusd falls back to Piper
                log.exception("say failed")
                return self._send(500, json.dumps({"error": str(e)[:300]}).encode())
            self._send(200, wav, "audio/wav")

    return H


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ari-voice", description="Ari's expressive voice (Chatterbox)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8611)
    p.add_argument("--model", choices=["turbo", "standard"], default="turbo",
                   help="turbo: fast, real laughs and sighs; standard: slower, stronger moods")
    p.add_argument("--device", default=None)
    p.add_argument("--warm", action="store_true", help="load the model now, not on the first sentence")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    engine = Engine(a.model, a.device)
    if a.warm:
        threading.Thread(target=engine.load, daemon=True).start()
    srv = ThreadingHTTPServer((a.host, a.port), handler(engine))
    log.info("ari voice listening", extra={"at": f"http://{a.host}:{a.port}"})
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
