"""Training Ari on your voice: the sentences, the recordings (Helios), measuring and the training run (no GPU,
no models: the trainer, converter and transcriber are stand-ins)."""

from __future__ import annotations

import urllib.request
from pathlib import Path

import numpy as np
import pytest

from argus import voice_train as vt
from argus.voice_corpus import sentences
from argus.voice_samples import Samples
from test_brief_weather import make
from test_worker import Server, client


def test_the_sentences_cover_your_names_and_keep_their_ids():
    s = sentences(["Kaancha"])
    assert 250 <= len(s) <= 400 and len({x["id"] for x in s}) == len(s)
    assert sum("Kaancha" in x["text"] for x in s) >= 4
    assert {x["kind"] for x in s[:10]} >= {"command", "talk", "numbers", "plain", "names"}  # mixed from the start
    assert sentences(["Kaancha"])[0] == s[0]


def test_word_error_rate_and_the_test_set():
    assert vt.wer(["open WhatsApp now"], ["open what's up now"]) == 2 / 3
    assert vt.wer(["Hi, Ari!"], ["hi ari"]) == 0
    items = [{"id": f"{i:012x}"} for i in range(100)]
    train, test = vt.split(items)
    assert 8 <= len(test) <= 20 and not {x["id"] for x in test} & {x["id"] for x in train}
    assert vt.split(items)[1] == test  # the same sentences every run


def test_recordings_from_helios(tmp_path):
    with Server(make(tmp_path).open()) as srv:
        cl = client(srv.url)
        st = cl.get("/voice-train")
        first = st["sentences"][0]
        assert st["recorded"] == 0 and st["goal_seconds"] == 1200 and not first["done"]
        req = urllib.request.Request(f"{srv.url}/voice-train/{first['id']}?seconds=2.4", data=b"RIFF" + b"\0" * 400,
                                     method="POST", headers={"Content-Type": "audio/wav"})
        urllib.request.urlopen(req)
        st = cl.get("/voice-train")
        assert st["recorded"] == 1 and st["seconds"] == 2.4 and st["sentences"][0]["done"]
        assert cl.get("/voice-train-samples")[first["id"]]["text"] == first["text"]
        assert urllib.request.urlopen(f"{srv.url}/voice-train/{first['id']}/audio").read().startswith(b"RIFF")
        assert cl.call("DELETE", f"/voice-train/{first['id']}")[1]["removed"] is True
        assert cl.get("/voice-train")["recorded"] == 0
    assert not list((tmp_path / "data" / "voice-train").glob("*.wav"))


def test_a_trained_model_is_kept_only_when_it_does_better(tmp_path):
    items = [{"id": f"{i:012x}", "text": f"open whatsapp number {i}", "audio": np.zeros(16000, np.float32)}
             for i in range(60)]
    seen = {}

    def trainer(hf_id, train, test, audio, out, **kw):
        seen.update(hf_id=hf_id, n=len(train), epochs=kw["epochs"])
        out.mkdir(parents=True)
        return {"best_test_loss": 0.5, "epochs": [], "improved": True}

    def converter(hf, ct):
        ct.mkdir(parents=True)
        (ct / "model.bin").write_text("x")

    def transcriber(better: bool):
        return lambda name, test, audio: [x["text"] if (better and "ct2" in name) else "open what's up"
                                          for x in test]

    r = vt.run(items, "small.en", tmp_path / "mine", work=tmp_path / "w", trainer=trainer, converter=converter,
               transcriber=transcriber(True), log=lambda *_: None)
    assert r["kept"] and r["wer_after"] == 0 and r["wer_before"] > 0.5
    assert seen["hf_id"] == "openai/whisper-small.en" and seen["n"] == len(items) - len(vt.split(items)[1])
    assert (tmp_path / "mine" / "model.bin").exists() and (tmp_path / "mine" / "trained.json").exists()
    r = vt.run(items, "small.en", tmp_path / "mine2", work=tmp_path / "w2", trainer=trainer, converter=converter,
               transcriber=transcriber(False), log=lambda *_: None)
    assert not r["kept"] and not (tmp_path / "mine2").exists()


def test_too_few_recordings_say_so(tmp_path):
    items = [{"id": f"{i:012x}", "text": "hi", "audio": np.zeros(16000, np.float32)} for i in range(10)]
    with pytest.raises(SystemExit, match="at least 40"):
        vt.run(items, "small.en", tmp_path / "m", work=tmp_path / "w", log=lambda *_: None)


def test_loads_recordings_from_the_folder(tmp_path):
    Samples(tmp_path).add("abc123abc123", "Hello there.", b"data", "audio/webm", 1.5)
    got = vt.load_local(tmp_path)
    assert got[0]["text"] == "Hello there." and Path(got[0]["path"]).read_bytes() == b"data"


def test_the_real_training_code_runs_end_to_end(tmp_path):
    """LoRA training, merging, conversion for faster-whisper and transcribing with it, on a tiny random model. In its
    own process: PyTorch and CTranslate2 loaded next to the rest of the test suite's libraries can crash."""
    import os
    import subprocess
    import sys

    if not os.environ.get("ARGUS_SLOW_TESTS"):
        pytest.skip("set ARGUS_SLOW_TESTS=1 (needs PyTorch, transformers, peft; about a minute)")
    for mod in ("torch", "transformers", "peft", "ctranslate2", "faster_whisper"):
        pytest.importorskip(mod)
    here = Path(__file__).parent
    script = f"""
import sys, numpy as np
from pathlib import Path
sys.path[:0] = [{str(here.parent / "core")!r}]
from argus import voice_train as vt
from argus.voice_corpus import sentences
tmp = Path({str(tmp_path)!r}); base = tmp / "tiny"
vt.convert(base, tmp / "tiny-ct2")
rng = np.random.default_rng(0)
items = [{{"id": f"{{i:012x}}", "text": s["text"], "audio": (rng.standard_normal(16000) * 0.05).astype(np.float32)}}
         for i, s in enumerate(sentences()[:44])]
tr = lambda name, t, a: vt.transcribe_all(str(tmp / "tiny-ct2") if name == str(base) else name, t, a)
r = vt.run(items, str(base), tmp / "mine", epochs=1, batch=4, work=tmp / "w", transcriber=tr, log=lambda *_: None)
assert r["epochs"] and "wer_after" in r
names = {{p.name for p in (tmp / "w" / "ct2").iterdir()}}
assert {{"model.bin", "tokenizer.json", "preprocessor_config.json"}} <= names, names
print("OK")
"""
    build = (f"from pathlib import Path\nexec(open({str(here / '_tiny_whisper.py')!r}).read())\n"
             f"tiny_whisper(Path({str(tmp_path / 'tiny')!r}))")
    b = subprocess.run([sys.executable, "-c", build], capture_output=True, text=True, timeout=300)
    assert b.returncode == 0, b.stderr[-2000:]
    p = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=900)
    assert "OK" in p.stdout, (p.stdout + p.stderr)[-2000:]  # (CTranslate2 may crash on exit here; the work is done)
