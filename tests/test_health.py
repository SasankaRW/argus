"""Ari's health check: what is wrong, said plainly. The services are made up (no network)."""

from __future__ import annotations

from argus import health
from argus.config import Config


def cfg(**ari):
    return Config.model_validate({
        "models": {"tiers": {"T1": {"provider": "ollama", "model": "qwen3:latest"}}, "chain": ["T1"]},
        "ari": {"voice_engine": "expressive", **ari}, "plugins": {"live": ["web"]}})


def fake(routes):
    def get(url, timeout=5):
        for part, ans in routes.items():
            if part in url:
                if isinstance(ans, Exception):
                    raise ans
                return ans
        raise OSError("nothing there")
    return get


GOOD = {
    "/api/tags": (200, {"models": [{"name": "qwen3:latest"}]}),
    "/api/ps": (200, {"models": [{"name": "qwen3:latest", "size": 100, "size_vram": 100}]}),
    "8888/search": (200, {"results": [{"title": "x"}]}),
    "8611/health": (200, {"ok": True, "model": "turbo", "device": "cuda", "loaded": True}),
}


def levels(r):
    return {c["name"]: c["level"] for c in r["checks"]}


def test_everything_fine():
    r = health.run(cfg(), tools=[], get=fake(GOOD), gpu_workers=1, now=1000.0, decide=lambda task: {})
    assert r["ok"] and levels(r)["ollama"] == "ok" and levels(r)["web search"] == "ok" and levels(r)["voice"] == "ok"
    assert levels(r)["tool picking"] == "ok"
    assert health.summary({**r, "checks": [c for c in r["checks"] if c["level"] == "ok"]}).startswith("Everything")


def test_what_is_wrong_is_said_with_what_to_do():
    routes = {**GOOD, "8888/search": OSError("refused"),
              "8611/health": (200, {"ok": True, "model": "turbo", "device": "cpu", "loaded": True}),
              "/api/ps": (200, {"models": [{"name": "qwen3:latest", "size": 100, "size_vram": 60}]})}
    r = health.run(cfg(listen=True), tools=[], get=fake(routes), listener_seen=0.0, gpu_workers=0, now=1000.0,
                   decide=lambda task: {})
    lv = levels(r)
    assert lv["web search"] == "bad" and lv["voice"] == "bad" and lv["model on the GPU"] == "warn"
    assert lv["listener"] == "warn" and lv["worker"] == "warn" and not r["ok"]
    voice = next(c for c in r["checks"] if c["name"] == "voice")
    assert "CUDA" in voice["fix"] and "cpu" in voice["detail"]
    text = health.summary(r)
    assert "web search" in text and "voice" in text


def test_ollama_down_or_a_model_not_pulled():
    r = health.ollama(cfg(), fake({}))
    assert r[0]["level"] == "bad" and "Ollama" in r[0]["fix"]
    r = health.ollama(cfg(), fake({"/api/tags": (200, {"models": []}), "/api/ps": (200, {"models": []})}))
    assert any(c["level"] == "bad" and "ollama pull qwen3:latest" in c["fix"] for c in r)


def test_a_voice_still_loading_and_piper():
    loading = {"8611/health": (200, {"ok": True, "model": "turbo", "device": None, "loaded": False})}
    assert health.voice(cfg(), fake(loading))["level"] == "warn"
    assert health.voice(Config.model_validate({}), fake({}))["level"] == "ok"


def test_searxng_refusing_json_is_named():
    c = health.searxng(cfg(), fake({"8888/search": (403, "forbidden")}))
    assert c["level"] == "bad" and "json" in c["fix"]


def test_tool_picking_counts_right_and_slow():
    tools = [{"name": n, "does": n} for n in ("phone_timer", "weather", "lab_status", "backup_status", "web_search")]
    answers = {"start a timer": "phone_timer", "weather": "weather", "laptop server": "web_search",
               "backup": "backup_status"}  # one wrong

    def decide(task):
        m = task["message"]
        for k, tool in answers.items():
            if k in m:
                return {"tool": tool}
        return {"need_web": True} if "cricket" in m else {}

    c = health.routing(cfg(), tools, decide)
    assert c["level"] in ("ok", "warn") and "right" in c["detail"] and "server" in c["detail"]


def test_tool_picking_keeps_numbers_for_the_trend():
    tools = [{"name": n, "does": n} for n in ("phone_timer", "weather", "lab_status", "backup_status", "web_search")]
    c = health.routing(cfg(), tools, lambda task: {})
    assert set(c["data"]) == {"right", "total", "p50", "direct"} and c["data"]["total"] >= 4
