"""Ari's voice and ears, and training her on your voice."""

from __future__ import annotations

import asyncio
import re
import time

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response

from ... import vocab
from ...ari import ari as ari_mod
from ...ids import new_id
from ...voice import voice_corpus
from ...voice.voice import Expressive
from ...voice.voice_samples import Samples
from ..schemas import AriSpeak
from ._deps import Deps


def build_router(deps: Deps) -> APIRouter:
    argus = deps.argus
    router = APIRouter()
    guarded = deps.guarded

    # Ari's voice and ears ----------------------------------------------------

    audio_dir = argus.cfg.db_path.parent / "ari"

    async def gpu_online() -> bool:
        return await able(["gpu"])

    async def able(needs: list[str]) -> bool:
        """An online worker offers all of `needs` (nothing needed: always)."""
        want = set(needs)
        return not want or any(w["state"] == "online" and want <= set(w["capabilities"])
                               for w in await argus.registry.workers())

    @router.get("/ari-voice", dependencies=guarded)
    async def ari_voice(request: Request) -> dict:
        """What Helios can use: Ari's voice (here, or on the PC while a GPU worker is online), Whisper on the PC."""
        host = request.client.host if request.client else "?"
        spoken = not expressive().remote or await gpu_online()
        return {"voice": spoken, "hearing": argus.cfg.ari.hearing,
                "whisper_ready": argus.cfg.ari.hearing == "whisper" and await gpu_online(),
                "popup_here": time.time() - deps.overlays.get(host, 0) < 75, "pill": argus.cfg.ari.pill}

    _expr: dict = {}

    def expressive():
        """Ari's voice client, made again when its address changes."""
        url = argus.cfg.ari.expressive_url
        if _expr.get("url") != url:
            tok = argus.cfg.secrets.worker_token  # the PC's voice asks for it from another machine
            _expr.update(url=url, client=Expressive(url, token=tok))
        return _expr["client"]

    @router.post("/ari-voice/say", dependencies=guarded)
    async def ari_say_audio(body: AriSpeak) -> Response:
        """Ari's voice: WAV audio of the text. 409 while it can't speak (the PC is off, the voice is loading):
        the words are shown and nothing else speaks."""
        if expressive().remote and not await gpu_online():
            raise HTTPException(status_code=409, detail="Ari's voice is on the PC, which is off")
        wav = await asyncio.to_thread(expressive().say, body.text)
        if wav is None:
            raise HTTPException(status_code=409, detail="Ari's voice isn't ready")
        return Response(wav, media_type="audio/wav", headers={"Cache-Control": "no-store"})

    @router.post("/ari-voice/hear", dependencies=guarded)
    async def ari_hear(request: Request) -> dict:
        """A recording in (webm/ogg/wav, at most 5 MB), its text out: Whisper on the PC. 409 while the PC is off
        (Helios then uses the browser's recognition)."""
        if argus.cfg.ari.hearing != "whisper":
            raise HTTPException(status_code=409, detail="hearing is set to the browser (ari.hearing)")
        if not await gpu_online():
            raise HTTPException(status_code=409, detail="the PC is off")
        data = bytearray()
        async for chunk in request.stream():
            data += chunk
            if len(data) > 5 * 1024 * 1024:
                raise HTTPException(status_code=413, detail="recording too long")
        if not data:
            raise HTTPException(status_code=422, detail="empty recording")
        kind = (request.headers.get("content-type") or "").split(";")[0].strip()
        ext = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/wav": "wav", "audio/x-wav": "wav",
               "audio/mp4": "m4a", "audio/mpeg": "mp3"}.get(kind, "webm")
        name = f"{new_id().lower()}.{ext}"
        audio_dir.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread((audio_dir / name).write_bytes, bytes(data))
        try:
            voc = await deps.vocabulary()
            job_id, _ = await argus.jobs.enqueue("ari", "transcribe", {"audio": name,
                                                                      "model": argus.cfg.ari.whisper_model,
                                                                      "prompt": voc["prompt"]},
                                                 needs=["gpu"], priority=95, source="helios")
            for _ in range(120):  # up to 60 s (the first time loads the model)
                j = await argus.jobs.get(job_id)
                if j.state.value == "succeeded":
                    return {"text": (j.result or {}).get("text", "")}
                if j.state.value in ("dead", "cancelled"):
                    raise HTTPException(status_code=502, detail=(j.error or "could not transcribe").split("\n")[0])
                await asyncio.sleep(0.5)
            await argus.jobs.cancel(job_id, "took too long")
            raise HTTPException(status_code=504, detail="the PC didn't answer in time")
        finally:
            (audio_dir / name).unlink(missing_ok=True)

    @router.get("/ari/audio/{name}", dependencies=guarded)
    async def ari_audio(name: str):
        if not re.fullmatch(r"[0-9a-z]{10,40}\.(webm|ogg|wav|m4a|mp3)", name) or not (audio_dir / name).is_file():
            raise HTTPException(status_code=404, detail="no such recording")
        return FileResponse(audio_dir / name, media_type="application/octet-stream")

    samples = Samples(argus.cfg.db_path.parent / "voice-train")

    async def corpus() -> list[dict]:
        facts = await argus.store.read(lambda c: [m["fact"] for m in ari_mod.memories(c)])
        mine = [w for w in vocab.words(argus.cfg.ari.vocabulary, facts) if w not in vocab.BUILTIN]
        return voice_corpus.sentences(mine)

    @router.get("/voice-train", dependencies=guarded)
    async def voice_train() -> dict:
        """The sentences to read, which are recorded, and how much (Helios > Ari > Train on my voice)."""
        have = samples.all()
        todo = await corpus()
        return {"sentences": [{**x, "done": x["id"] in have, "seconds": have.get(x["id"], {}).get("seconds")}
                              for x in todo],
                "recorded": len(have), "seconds": round(sum(v["seconds"] for v in have.values()), 1),
                "goal_seconds": 20 * 60, "model": argus.cfg.ari.whisper_model}

    @router.post("/voice-train/{sid}", dependencies=guarded)
    async def voice_train_add(sid: str, request: Request, seconds: float = Query(..., gt=0.3, le=30)) -> dict:
        """One sentence read aloud (webm/ogg/wav, at most 3 MB). Recording it again replaces it."""
        hit = next((x for x in await corpus() if x["id"] == sid), None)
        if hit is None:
            raise HTTPException(status_code=404, detail="no such sentence")
        data = bytearray()
        async for chunk in request.stream():
            data += chunk
            if len(data) > 3 * 1024 * 1024:
                raise HTTPException(status_code=413, detail="recording too long")
        if len(data) < 200:
            raise HTTPException(status_code=422, detail="empty recording")
        kind = (request.headers.get("content-type") or "").split(";")[0].strip()
        return await asyncio.to_thread(samples.add, sid, hit["text"], bytes(data), kind, seconds)

    @router.delete("/voice-train/{sid}", dependencies=guarded)
    async def voice_train_remove(sid: str) -> dict:
        return {"removed": await asyncio.to_thread(samples.remove, sid)}

    @router.get("/voice-train/{sid}/audio", dependencies=guarded)
    async def voice_train_audio(sid: str):
        """A recording (to play it back in Helios, or for the trainer on another machine)."""
        p = samples.path(sid) if re.fullmatch(r"[0-9a-f]{12}", sid) else None
        if p is None or not p.is_file():
            raise HTTPException(status_code=404, detail="no such recording")
        return FileResponse(p, media_type={"webm": "audio/webm", "ogg": "audio/ogg", "wav": "audio/wav",
                                           "m4a": "audio/mp4"}.get(p.suffix[1:], "application/octet-stream"))

    @router.get("/voice-train-samples", dependencies=guarded)
    async def voice_train_samples() -> dict:
        """Every recording with its sentence (for `python -m argus.voice.voice_train --from <argus url>`)."""
        return samples.all()

    deps.able = able
    deps.expressive = expressive
    return router
