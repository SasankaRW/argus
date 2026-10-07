"""Ari's expressive voice: Chatterbox (Resemble AI, MIT) on the PC's GPU, as a tiny local web service.

It runs in its own Python environment (Chatterbox pins its own PyTorch, NumPy and Transformers), so argusd talks to
it over HTTP. The supervisor starts it when ari.voice_engine is "expressive" (docs/setup.md has the setup):

    .venv-voice\\Scripts\\python core\\argus\\voice_server.py        (listens on 127.0.0.1:8611)

POST /say {"text": "[cheerful] Oh nice! [laugh] ...", "clip": "data/voices/ari-clip.wav"} -> WAV
  The mood sets how lively it sounds, [laugh] / [sigh] / [chuckle] / [gasp] / [groan] / [clear throat] are real
  sounds. "clip" (optional, 5+ seconds) is the voice to sound like; without it, Chatterbox's own voice.
GET /health -> {"ok": true, "model": "turbo", "device": "cuda"}

With --host 0.0.0.0 (ari.expressive_share, for Argus on the laptop) a request from another machine needs the
worker token (ARGUS_WORKER_TOKEN, from .env) as "Authorization: Bearer <token>"; this PC itself needs none.

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


DEFAULT_CLIP = Path("data") / "voices" / "ari-clip.wav"  # this PC's clip, when the asker's isn't here


def worker_token(env_file: Path = Path(".env")) -> str | None:
    """ARGUS_WORKER_TOKEN from the environment or .env (this Python has no argus.config: plain parsing)."""
    import os

    tok = os.environ.get("ARGUS_WORKER_TOKEN")
    if tok:
        return tok
    try:
        for line in env_file.read_text(encoding="utf-8-sig").splitlines():
            k, _, v = line.partition("=")
            if k.strip() == "ARGUS_WORKER_TOKEN" and v.strip():
                return v.strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def allowed(client_ip: str, authorization: str | None, token: str | None) -> bool:
    """This PC always; another machine only with the worker token (and never when no token is set)."""
    import hmac

    if client_ip in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
        return True
    return bool(token) and hmac.compare_digest((authorization or "").encode(), f"Bearer {token}".encode())


def their_clip(clip: str | None) -> str | None:
    """The clip asked for if it is on this PC, else this PC's own (the laptop sends a path of its own)."""
    if clip and Path(clip).is_file():
        return clip
    return str(DEFAULT_CLIP.resolve()) if DEFAULT_CLIP.is_file() else None


def handler(engine, token: str | None = None) -> type[BaseHTTPRequestHandler]:
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
            if not allowed(self.client_address[0], self.headers.get("Authorization"), token):
                n = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(min(n, 20000))
                return self._send(401, b'{"error": "the worker token is needed from another machine"}')
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(min(n, 20000)) or b"{}")
                text = str(body.get("text") or "")[:1000]
                wav = engine.say(text, their_clip(str(body.get("clip") or "") or None))
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
    token = worker_token()
    if a.host not in ("127.0.0.1", "localhost") and not token:
        log.warning("no ARGUS_WORKER_TOKEN: only this PC can use the voice")
    srv = ThreadingHTTPServer((a.host, a.port), handler(engine, token))
    log.info("ari voice listening", extra={"at": f"http://{a.host}:{a.port}"})
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
