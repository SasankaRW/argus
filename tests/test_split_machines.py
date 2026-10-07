"""Argus on the laptop, models on the PC: model work goes only to the PC's worker (and Ari says the PC is being
woken), plugins can be pinned to the machine their service runs on, --check warns about the rest, and the PC's
listener makes Ari's voice itself."""

from __future__ import annotations

from pathlib import Path

import yaml

from argus import ari_listen
from argus.config import Config, load_config
from argus.plugins import discover, placement_warnings
from argus.worker import Worker
from fakes import FakeOllama
from test_ari_think import make, settle
from test_worker import Server, client, wait_for

ROOT = Path(__file__).resolve().parents[1]

WEB = """id: webby
name: Webby
version: 1.0.0
kind: workflow
argus_api: ">=1.0 <2.0"
runs_on: any
permissions: {models: []}
triggers:
  - manual: {workflow: go, label: Go}
config:
  searx_url: {type: text, default: "http://127.0.0.1:8888", label: SearXNG}
"""


def test_the_laptop_template_sends_model_work_to_the_pc():
    cfg = Config.model_validate(yaml.safe_load((ROOT / "deploy/linux/argus.laptop.yaml").read_text()))
    assert cfg.models.needs == ["gpu"]
    assert cfg.plugins.runs_on["web"] == "desktop" and cfg.ari.voice_engine == "piper"
    assert Config().models.needs == []  # one machine (the PC): any worker, as before


def test_ari_waits_for_the_pc_and_the_laptop_worker_never_takes_it(tmp_path):
    with FakeOllama({"qwen2.5-coder:7b": [{"reply": "Hi from the PC."}]}) as ol, \
            Server(make(tmp_path, "  chain: [T1]\n  needs: [gpu]\n").open()) as srv:
        cl = client(srv.url)
        laptop = Worker(cl, "laptop", capabilities=["laptop"], ollama_url=ol.url, watch_folders=False)
        laptop.register()
        r = cl.post("/ari", {"text": "write me a short poem about rain"})
        assert cl.get(f"/jobs/{r['job_id']}")["needs"] == ["gpu"]
        state = cl.get("/events?kinds=ari.state&newest=1&limit=1")["events"][-1]["data"]
        assert state["phase"] == "thinking" and "Waking the PC" in state["text"]
        assert laptop.run_once(wait=0) is False  # no models there: it isn't the laptop's job
        pc = Worker(cl, "pc", capabilities=["desktop", "gpu"], ollama_url=ol.url, watch_folders=False)
        pc.register()
        assert settle(cl, pc, r["conv"])["text"] == "Hi from the PC."
        r2 = cl.post("/ari", {"text": "and another poem please"})  # the PC is up now: no waking note
        state = cl.get("/events?kinds=ari.state&newest=1&limit=1")["events"][-1]["data"]
        assert state["text"] == "and another poem please" and r2["job_id"]


def write(tmp_path: Path, host: str, extra: str = "") -> Config:
    pdir = tmp_path / "plugins" / "webby"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "plugin.yaml").write_text(WEB)
    (pdir / "plugin.py").write_text("")
    (tmp_path / "argus.yaml").write_text(
        f"instance: {{host: {host}}}\nlogging:\n  file: null\n"
        f"plugins:\n  dirs: ['{(tmp_path / 'plugins').as_posix()}']\n  live: [webby]\n{extra}", encoding="utf-8")
    return load_config(tmp_path / "argus.yaml")


def test_a_plugin_is_pinned_where_its_service_runs(tmp_path):
    cfg = write(tmp_path, "laptop")
    (p,), _ = discover(cfg)
    assert p.manifest.runs_on == "any" and p.manifest.job_needs() == []
    warns = placement_warnings(cfg, [p])
    assert any("models.needs" in w for w in warns)
    assert any("webby: searx_url=http://127.0.0.1:8888" in w and "runs_on: {webby: desktop}" in w for w in warns)

    cfg = write(tmp_path, "laptop", "  runs_on: {webby: desktop}\nmodels: {needs: [gpu]}\n")
    (p,), _ = discover(cfg)
    assert p.manifest.runs_on == "desktop" and p.manifest.job_needs() == ["desktop"]
    assert placement_warnings(cfg, [p]) == []
    assert placement_warnings(write(tmp_path, "pc"), discover(write(tmp_path, "pc"))[0]) == []  # one machine


def test_the_listener_speaks_with_this_pcs_voice_first(tmp_path):
    calls = []
    c = ari_listen.AriClient("http://127.0.0.1:9", None, tmp_path / "conv", local=lambda t: calls.append(t) or b"WAV")
    assert c.voice("hello") == b"WAV" and calls == ["hello"]  # never asked argusd (port 9: nothing there)

    (tmp_path / "argus.yaml").write_text("logging:\n  file: null\n", encoding="utf-8")
    assert ari_listen.local_voice(load_config(tmp_path / "argus.yaml")) is None  # no voice here: argusd's
    (tmp_path / "argus.yaml").write_text("logging:\n  file: null\nari:\n  voice: data/voices/none.onnx\n",
                                         encoding="utf-8")
    say = ari_listen.local_voice(load_config(tmp_path / "argus.yaml"))
    assert say is not None and say("hi") is None  # Piper set but its file missing: falls back to argusd


def test_ari_health_checks_the_pcs_services_on_the_pc(tmp_path, monkeypatch):
    import threading

    monkeypatch.setenv("ARGUS_CONFIG", str(tmp_path / "missing.yaml"))  # the PC's own argus.yaml (none here)
    with FakeOllama({"qwen2.5-coder:7b": []}) as ol, \
            Server(make(tmp_path, "  chain: [T1]\n  needs: [gpu]\n").open()) as srv:
        cl = client(srv.url)
        laptop = Worker(cl, "laptop", capabilities=["laptop"], ollama_url=ol.url, watch_folders=False)
        laptop.register()
        checks = cl.post("/ari/health", {})["health"]["checks"]
        assert checks[0]["name"] == "the PC" and "off" in checks[0]["detail"]  # never woken for a check
        assert not any(c["name"] in ("ollama", "web search") for c in checks)  # not the laptop's 127.0.0.1

        pc = Worker(cl, "pc", capabilities=["desktop", "gpu"], ollama_url=ol.url, watch_folders=False)
        pc.register()
        got: dict = {}
        t = threading.Thread(target=lambda: got.update(cl.post("/ari/health", {})))
        t.start()
        wait_for(lambda: pc.run_once(wait=0.5))
        t.join(30)
        names = [c["name"] for c in got["health"]["checks"]]
        assert "ollama" in names and "web search" in names and "the PC" not in names
        assert cl.get("/jobs?plugin=ari")[0]["needs"] == ["gpu"]


def test_the_pc_voice_follows_the_voice_picked_in_helios(tmp_path, monkeypatch):
    import json
    import time

    from argus import voice as voice_mod

    voices = tmp_path / "data" / "voices"
    voices.mkdir(parents=True)
    for name in ("en_GB-cori-high", "en_US-amy-medium"):
        (voices / f"{name}.onnx").write_bytes(b"x")
        (voices / f"{name}.onnx.json").write_text("{}")
    (voices / "ari-clip.wav").write_bytes(b"OLD")
    used: list[str] = []
    monkeypatch.setattr(voice_mod.Voice, "use", lambda self, path, speed=None: used.append(path.stem))
    monkeypatch.setattr(voice_mod.Voice, "say", lambda self, text: b"NEWCLIP")
    monkeypatch.setattr(voice_mod.Expressive, "warm", lambda self: True)
    monkeypatch.setattr(voice_mod.Expressive, "say", lambda self, text: b"GPU")
    (tmp_path / "argus.yaml").write_text("logging:\n  file: null\nari:\n  voice_engine: expressive\n"
                                         "  voice: data/voices/en_GB-cori-high.onnx\n", encoding="utf-8")
    cfg = load_config(tmp_path / "argus.yaml")

    say = ari_listen.local_voice(cfg, lambda: {"current": "en_GB-cori-high", "speed": 1.1})
    assert say("hello") == b"GPU"  # the expressive voice on this PC
    wait_for(lambda: (voices / "ari-clip.json").exists())
    assert (voices / "ari-clip.wav").read_bytes() == b"OLD"  # your existing clip is kept
    assert json.loads((voices / "ari-clip.json").read_text())["voice"] == "en_GB-cori-high"

    ari_listen.local_voice(cfg, lambda: {"current": "en_US-amy-medium", "speed": 1.0})  # you picked another
    wait_for(lambda: (voices / "ari-clip.wav").read_bytes() == b"NEWCLIP")
    time.sleep(0.05)
    assert json.loads((voices / "ari-clip.json").read_text())["voice"] == "en_US-amy-medium"
    assert used[-1] == "en_US-amy-medium"


def test_ari_never_calls_you_friend():
    from argus.worker.think import CHAT, PERSONA, no_helpdesk

    assert no_helpdesk("Stay dry, friend.") == "Stay dry."
    assert no_helpdesk("Hey buddy, it's 7 pm.") == "Hey, it's 7 pm."
    assert no_helpdesk("My friend Kasun called.") == "My friend Kasun called."  # a friend you talk about stays
    assert no_helpdesk("You're a good friend.") == "You're a good friend."
    assert "never call them \"friend\"" in PERSONA and "and friend" not in CHAT


def test_the_worker_picks_up_new_settings_without_a_restart(tmp_path, monkeypatch):
    with FakeOllama({"qwen2.5-coder:7b": []}) as ol, Server(make(tmp_path).open()) as srv:
        cl = client(srv.url)
        w = Worker(cl, "pc", capabilities=["desktop", "gpu"], ollama_url=ol.url, watch_folders=False)
        w.register()
        first = w._registered
        w.run_once(wait=0)
        assert w._registered == first  # not every time
        monkeypatch.setattr(Worker, "REGISTER_SECONDS", 0.0)
        w.run_once(wait=0)
        assert w._registered > first  # argusd's settings (models, plugins) asked for again


def test_the_pcs_expressive_voice_is_shared_with_the_laptop_safely(tmp_path, monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location("voice_server", ROOT / "core/argus/voice_server.py")
    vs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vs)
    assert vs.allowed("127.0.0.1", None, "t")  # this PC: as before
    assert vs.allowed("100.98.211.7", "Bearer t", "t")  # the laptop, with the token
    assert not vs.allowed("100.98.211.7", None, "t") and not vs.allowed("100.98.211.7", "Bearer x", "t")
    assert not vs.allowed("100.98.211.7", "Bearer ", None)  # no token set: only this PC
    (tmp_path / ".env").write_text("ARGUS_WORKER_TOKEN=abc\n")
    monkeypatch.delenv("ARGUS_WORKER_TOKEN", raising=False)
    assert vs.worker_token(tmp_path / ".env") == "abc"
    monkeypatch.chdir(tmp_path)
    assert vs.their_clip("/opt/argus/data/voices/ari-clip.wav") is None  # the laptop's path, no clip here
    (tmp_path / "data" / "voices").mkdir(parents=True)
    (tmp_path / "data" / "voices" / "ari-clip.wav").write_bytes(b"RIFF")
    assert vs.their_clip("/opt/argus/data/voices/ari-clip.wav").endswith("ari-clip.wav")  # this PC's own


def test_helios_voice_doesnt_wait_for_a_pc_that_is_off(tmp_path):
    import time

    import httpx

    srv_args = "  chain: [T1]\n  needs: [gpu]\nari:\n  voice_engine: expressive\n" \
               "  expressive_url: http://10.255.255.1:8611\n"
    with Server(make(tmp_path, srv_args).open()) as srv:
        t0 = time.monotonic()
        r = httpx.post(srv.url + "/ari-voice/say", json={"text": "hello"}, timeout=20)
        assert r.status_code == 409 and time.monotonic() - t0 < 3  # Piper here (none set), not a 30 s wait
