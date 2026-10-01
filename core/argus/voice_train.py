"""Train Ari's hearing on your voice: Whisper fine-tuned on the sentences you read in Helios (Ari > Train on my
voice), on this PC's GPU. Run it on the PC:

    pip install -e .[train]                      (and PyTorch for CUDA, see docs/setup.md)
    python -m argus.voice_train                  (about 20-60 minutes on an RTX 5070)
    python -m argus.voice_train --apply          (... and switch Ari to the new model when it is better)

What it does:
1. Reads your recordings (data/voice-train, or `--from http://laptop:8600` when Argus runs elsewhere).
2. Keeps one in ten aside to test with, and measures how often today's Whisper gets your words wrong (word error
   rate, WER).
3. Fine-tunes Whisper (`--base`, default ari.whisper_model) with LoRA: small extra weights learn your accent and
   your names, the original model stays as it is, so 15-30 minutes of speech is enough without it forgetting
   English. Your voice is sped up/down and mixed with a little noise as it goes, so it doesn't just memorise.
4. Merges, converts it for faster-whisper (CTranslate2, float16) and measures the error rate again on the set-aside
   sentences. Only a model that does better is kept: data/models/whisper-mine (and with --apply, Ari uses it).

Nothing leaves the PC except the one-time download of the base model from Hugging Face.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import shutil
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

HF = {"tiny.en": "openai/whisper-tiny.en", "base.en": "openai/whisper-base.en", "small.en": "openai/whisper-small.en",
      "medium.en": "openai/whisper-medium.en", "tiny": "openai/whisper-tiny", "base": "openai/whisper-base",
      "small": "openai/whisper-small", "medium": "openai/whisper-medium", "large-v3": "openai/whisper-large-v3",
      "large-v3-turbo": "openai/whisper-large-v3-turbo", "turbo": "openai/whisper-large-v3-turbo"}
RATE = 16000


# ------------------------------------------------------------------ measuring (no torch needed)

def words(text: str) -> list[str]:
    t = text.lower().replace("’", "'")
    t = re.sub(r"[^\w\s']", " ", t)
    return t.split()


def wer(refs: list[str], hyps: list[str]) -> float:
    """Word error rate over all sentences: (substitutions + deletions + insertions) / words said."""
    errors = total = 0
    for r, h in zip(refs, hyps, strict=True):
        a, b = words(r), words(h)
        d = list(range(len(b) + 1))
        for i in range(1, len(a) + 1):
            prev, d[0] = d[0], i
            for j in range(1, len(b) + 1):
                cur = min(d[j] + 1, d[j - 1] + 1, prev + (a[i - 1] != b[j - 1]))
                prev, d[j] = d[j], cur
        errors += d[len(b)]
        total += len(a)
    return errors / max(total, 1)


def split(items: list[dict]) -> tuple[list[dict], list[dict]]:
    """(train, test): one in ten set aside by sentence id (the same ones every run), at least 8 when possible."""
    test = [x for x in items if int(hashlib.sha1(x["id"].encode()).hexdigest()[:4], 16) % 10 == 0]
    if len(test) < 8 and len(items) >= 40:
        rest = [x for x in items if x not in test]
        test += rest[: 8 - len(test)]
    train = [x for x in items if x not in test]
    return train, test


# ------------------------------------------------------------------ the recordings

def load_local(folder: Path) -> list[dict]:
    try:
        idx = json.loads((folder / "samples.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [{**v, "path": folder / v["file"]} for v in idx.values() if (folder / v["file"]).is_file()]


def load_remote(url: str, token: str | None, cache: Path) -> list[dict]:
    """The recordings from an Argus on another machine, kept in `cache` (only new ones are fetched)."""
    def get(path: str) -> bytes:
        req = urllib.request.Request(url.rstrip("/") + path,
                                     headers={"Authorization": f"Bearer {token}"} if token else {})
        with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310 - the user's own Argus
            return r.read()

    idx = json.loads(get("/voice-train-samples"))
    cache.mkdir(parents=True, exist_ok=True)
    out = []
    for v in idx.values():
        p = cache / v["file"]
        if not p.exists():
            p.write_bytes(get(f"/voice-train/{v['id']}/audio"))
        out.append({**v, "path": p})
    return out


def audio_of(item: dict):
    from .worker.hear import decode

    return decode(Path(item["path"]).read_bytes())


# ------------------------------------------------------------------ transcribing with faster-whisper

def transcribe_all(model_name: str, items: list[dict], audio: dict[str, Any]) -> list[str]:
    from faster_whisper import WhisperModel  # type: ignore[import-not-found]

    try:
        m = WhisperModel(model_name, device="cuda", compute_type="float16")
    except Exception:  # noqa: BLE001 - no CUDA for CTranslate2: the CPU is fine for a test set
        m = WhisperModel(model_name, device="cpu", compute_type="int8")
    out = []
    for x in items:
        segs, _ = m.transcribe(audio[x["id"]], language="en", beam_size=1, condition_on_previous_text=False,
                               without_timestamps=True, max_new_tokens=120)
        out.append(" ".join(s.text.strip() for s in segs).strip())
    del m
    return out


def free_gpu(ollama: str) -> None:
    """Unload Ollama's models so training has the GPU's memory (they load again on the next question)."""
    try:
        with urllib.request.urlopen(ollama.rstrip("/") + "/api/ps", timeout=5) as r:  # noqa: S310
            loaded = [m["name"] for m in json.loads(r.read()).get("models") or []]
        for name in loaded:
            body = json.dumps({"model": name, "keep_alive": 0}).encode()
            urllib.request.urlopen(urllib.request.Request(ollama.rstrip("/") + "/api/generate", data=body,  # noqa: S310
                                                          headers={"Content-Type": "application/json"}), timeout=30)
        if loaded:
            print(f"  freed the GPU: unloaded {', '.join(loaded)} from Ollama")
    except Exception:  # noqa: BLE001 - Ollama not running: nothing to free
        pass


# ------------------------------------------------------------------ fine-tuning (torch, transformers, peft)

def augment(a, rng: random.Random):
    """A little variety each epoch: louder/quieter, 0.9-1.1x speed, some background hiss."""
    import numpy as np

    a = a * rng.uniform(0.6, 1.4)
    if rng.random() < 0.5:
        f = rng.uniform(0.9, 1.1)
        a = np.interp(np.arange(0, len(a), f), np.arange(len(a)), a)
    if rng.random() < 0.5:
        p = float(np.mean(a ** 2)) + 1e-9
        snr = rng.uniform(15, 35)
        a = a + np.random.default_rng(rng.randint(0, 1 << 30)).normal(0, (p / 10 ** (snr / 10)) ** 0.5, len(a))
    return np.clip(a, -1, 1).astype(np.float32)


def finetune(hf_id: str, train: list[dict], test: list[dict], audio: dict[str, Any], out: Path, *, epochs: int,
             lr: float, batch: int, rank: int, log=print) -> dict:
    """LoRA fine-tune; the merged model (Hugging Face format) is saved in `out`. Returns the losses."""
    import torch
    from peft import LoraConfig, get_peft_model  # type: ignore[import-not-found]
    from transformers import (  # type: ignore[import-not-found]
        WhisperForConditionalGeneration,
        WhisperProcessor,
        WhisperTokenizerFast,
    )

    dev = "cuda" if torch.cuda.is_available() else "cpu"
    if dev == "cpu":
        log("  no CUDA GPU for PyTorch: training on the CPU (slow; see docs/setup.md for the CUDA build)")
    proc = WhisperProcessor.from_pretrained(hf_id)
    if not hf_id.endswith(".en") and not Path(hf_id).exists():
        proc.tokenizer.set_prefix_tokens(language="english", task="transcribe")  # multilingual: say it's English
    model = WhisperForConditionalGeneration.from_pretrained(hf_id)
    model.config.forced_decoder_ids = None
    model.generation_config.forced_decoder_ids = None
    model = get_peft_model(model, LoraConfig(r=rank, lora_alpha=rank * 2, lora_dropout=0.05,
                                             target_modules=["q_proj", "k_proj", "v_proj", "out_proj", "fc1", "fc2"]))
    model.to(dev)
    tok = proc.tokenizer
    start = model.config.decoder_start_token_id

    def labels_of(text: str) -> list[int]:
        ids = tok(" " + text.strip()).input_ids
        return ids[1:] if ids and ids[0] == start else ids  # the model puts the start token back itself

    def batch_of(items: list[dict], rng: random.Random | None):
        feats = proc.feature_extractor([augment(audio[x["id"]], rng) if rng else audio[x["id"]] for x in items],
                                       sampling_rate=RATE, return_tensors="pt").input_features
        labs = [labels_of(x["text"]) for x in items]
        n = max(len(x) for x in labs)
        lab = torch.full((len(labs), n), -100, dtype=torch.long)
        for i, x in enumerate(labs):
            lab[i, :len(x)] = torch.tensor(x)
        return feats.to(dev), lab.to(dev)

    steps = epochs * ((len(train) + batch - 1) // batch)
    opt = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad], lr=lr, weight_decay=0.01)
    warm = max(1, steps // 10)
    sched = torch.optim.lr_scheduler.LambdaLR(opt, lambda s: min(1.0, (s + 1) / warm) * max(0.05, 1 - s / steps))
    amp = dev == "cuda"
    scaler = torch.amp.GradScaler("cuda", enabled=amp)
    rng = random.Random(7)

    def test_loss() -> float:
        model.eval()
        tot = n = 0
        with torch.no_grad(), torch.autocast(dev, enabled=amp, dtype=torch.float16):
            for i in range(0, len(test), batch):
                f, lab = batch_of(test[i:i + batch], None)
                tot += float(model(input_features=f, labels=lab).loss) * len(lab)
                n += len(lab)
        model.train()
        return tot / max(n, 1)

    best, best_state, history = test_loss(), None, []
    log(f"  test loss before: {best:.3f}")
    model.train()
    step, t0 = 0, time.time()
    for ep in range(epochs):
        rng.shuffle(train)
        for i in range(0, len(train), batch):
            f, lab = batch_of(train[i:i + batch], rng)
            with torch.autocast(dev, enabled=amp, dtype=torch.float16):
                loss = model(input_features=f, labels=lab).loss
            opt.zero_grad(set_to_none=True)
            scaler.scale(loss).backward()
            scaler.unscale_(opt)
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            scaler.step(opt)
            scaler.update()
            sched.step()
            step += 1
        tl = test_loss()
        history.append({"epoch": ep + 1, "test_loss": round(tl, 4)})
        log(f"  epoch {ep + 1}/{epochs}: test loss {tl:.3f} ({(time.time() - t0) / 60:.1f} min)")
        if tl < best:
            best = tl
            best_state = {k: v.detach().clone() for k, v in model.state_dict().items() if "lora_" in k}
    if best_state is not None:
        model.load_state_dict(best_state, strict=False)
    merged = model.merge_and_unload()
    out.mkdir(parents=True, exist_ok=True)
    merged.save_pretrained(out)
    proc.save_pretrained(out)
    proc.feature_extractor.save_pretrained(out)  # preprocessor_config.json: faster-whisper reads the mel size there
    WhisperTokenizerFast.from_pretrained(hf_id).save_pretrained(out)  # tokenizer.json, which faster-whisper reads
    return {"best_test_loss": round(best, 4), "epochs": history, "improved": best_state is not None}


def convert(hf_dir: Path, out: Path) -> None:
    """Hugging Face Whisper -> CTranslate2 (what faster-whisper loads)."""
    from ctranslate2.converters import TransformersConverter  # type: ignore[import-not-found]

    files = [f for f in ("tokenizer.json", "preprocessor_config.json") if (hf_dir / f).exists()]
    TransformersConverter(str(hf_dir), copy_files=files).convert(str(out), quantization="float16", force=True)


# ------------------------------------------------------------------ the whole run

def run(items: list[dict], base: str, out: Path, *, epochs: int = 6, lr: float = 5e-4, batch: int = 8,
        rank: int = 32, work: Path, log=print, trainer=finetune, converter=convert, transcriber=transcribe_all
        ) -> dict:
    hf_id = HF.get(base, base)
    good = []
    audio: dict[str, Any] = {}
    for x in items:
        try:
            a = audio_of(x) if "audio" not in x else x["audio"]
        except Exception as e:  # noqa: BLE001 - one broken recording doesn't stop the rest
            log(f"  skipped {x['id']}: {str(e)[:80]}")
            continue
        if 0.4 * RATE <= len(a) <= 30 * RATE:
            audio[x["id"]] = a
            good.append(x)
    minutes = sum(len(a) for a in audio.values()) / RATE / 60
    if len(good) < 40:
        raise SystemExit(f"Only {len(good)} usable recordings ({minutes:.1f} min): record at least 40 sentences "
                         "(10 minutes is better) in Helios > Ari > Train on my voice.")
    train, test = split(good)
    log(f"{len(good)} recordings, {minutes:.1f} min: {len(train)} to learn from, {len(test)} to test with")
    refs = [x["text"] for x in test]
    log(f"Measuring {base} on your voice ...")
    before_h = transcriber(base, test, audio)
    before = wer(refs, before_h)
    log(f"  word error rate now: {before:.1%}")
    hf_dir, ct_dir = work / "hf", work / "ct2"
    log(f"Fine-tuning {hf_id} ({epochs} passes) ...")
    losses = trainer(hf_id, train, test, audio, hf_dir, epochs=epochs, lr=lr, batch=batch, rank=rank, log=log)
    log("Converting for faster-whisper ...")
    converter(hf_dir, ct_dir)
    after_h = transcriber(str(ct_dir), test, audio)
    after = wer(refs, after_h)
    log(f"  word error rate after: {after:.1%} (was {before:.1%})")
    report = {"base": base, "recordings": len(good), "minutes": round(minutes, 1), "wer_before": round(before, 4),
              "wer_after": round(after, 4), "trained_at": time.strftime("%Y-%m-%d %H:%M"), **losses,
              "examples": [{"said": r, "before": b, "after": a} for r, b, a in zip(refs, before_h, after_h,
                                                                                  strict=True)
                           if b != a][:12]}
    better = after < before
    if better:
        if out.exists():
            shutil.rmtree(out)
        shutil.copytree(ct_dir, out)
        (out / "trained.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    report["kept"] = better
    return report


def main(argv: list[str] | None = None) -> int:
    from .config import load_config, parse_env_file

    p = argparse.ArgumentParser(prog="argus-voice-train", description="Fine-tune Whisper on your voice")
    p.add_argument("--base", default=None, help="Whisper to start from (default: ari.whisper_model, e.g. small.en)")
    p.add_argument("--from", dest="src", default=None, help="Argus on another machine, e.g. http://laptop:8600")
    p.add_argument("--out", default=None, help="where the trained model goes (default data/models/whisper-mine)")
    p.add_argument("--epochs", type=int, default=6)
    p.add_argument("--lr", type=float, default=5e-4)
    p.add_argument("--batch", type=int, default=8)
    p.add_argument("--rank", type=int, default=32, help="LoRA rank (bigger learns more, overfits sooner)")
    p.add_argument("--apply", action="store_true", help="when it is better, make Ari use it (ari.whisper_model)")
    p.add_argument("--keep-gpu", action="store_true", help="don't unload Ollama's models first")
    a = p.parse_args(argv)
    cfg = load_config()
    data = cfg.db_path.parent
    base = a.base or cfg.ari.whisper_model
    if Path(base).exists():
        raise SystemExit(f"--base {base} is a trained model already: start from the original (e.g. --base small.en)")
    out = Path(a.out) if a.out else data / "models" / "whisper-mine"
    token = os.environ.get("ARGUS_WORKER_TOKEN") or parse_env_file(Path(".env")).get("ARGUS_WORKER_TOKEN")
    items = load_remote(a.src, token, data / "voice-train-cache") if a.src else load_local(data / "voice-train")
    if not a.keep_gpu:
        free_gpu(cfg.ollama.url)
    work = data / "voice-train-work"
    try:
        r = run(items, base, out, epochs=a.epochs, lr=a.lr, batch=a.batch, rank=a.rank, work=work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    for ex in r["examples"][:6]:
        print(f"    said:   {ex['said']}\n    before: {ex['before']}\n    after:  {ex['after']}")
    if not r["kept"]:
        print("\nNot better than today's model, so nothing changed. Record more sentences (or try --epochs 10).")
        return 1
    rel = out.relative_to(Path.cwd()) if out.is_relative_to(Path.cwd()) else out
    print(f"\nKept: {rel} ({r['wer_before']:.1%} -> {r['wer_after']:.1%} word errors)")
    if a.apply:
        url = a.src or os.environ.get("ARGUS_URL", "http://127.0.0.1:8600")
        try:
            req = urllib.request.Request(url.rstrip("/") + "/argus-settings", method="PUT",
                                         data=json.dumps({"ari.whisper_model": rel.as_posix()}).encode(),
                                         headers={"Content-Type": "application/json",
                                                  **({"Authorization": f"Bearer {token}"} if token else {})})
            urllib.request.urlopen(req, timeout=10)  # noqa: S310 - the user's own Argus
            print("Ari now hears with it (restart ari-listen, or `dev.ps1 up`, for the microphone).")
        except Exception as e:  # noqa: BLE001
            print(f"Couldn't switch it in Argus ({e}); set ari.whisper_model: {rel.as_posix()} in argus.yaml.")
    else:
        print(f"Use it: Helios > Settings > Ari's Whisper model = {rel.as_posix()} (or run again with --apply).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
