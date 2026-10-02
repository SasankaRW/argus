"""Ari's health check: the things that quietly broke before (Ollama or its model, SearXNG, the voice on the GPU,
Whisper on the GPU, the listener) and does Ari still pick the right tool. Each check says ok / warn / bad in a
sentence and what to do about it. Run from Helios (Settings > Ari > Check Ari) and once a day (`health.at`); a
message goes to the phone only when something is wrong. Nothing here changes anything."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from .config import Config

Check = dict[str, Any]  # {name, level: ok|warn|bad, detail, fix}

SMOKE: list[tuple[str, str]] = [  # a few questions the local model has to answer by picking a tool (or not)
    ("start a timer for 10 minutes on my phone", "phone_timer"),
    ("where is my phone", "where_is_my_phone|ring_phone|ring_phone_now|phone_status"),
    ("what's the weather like", "weather"),
    ("find my CV", "search_everything|find_file|search_my_files"),
    ("is the laptop server up", "lab_status"),
    ("how much did I spend this month", "money_this_month"),
    ("is the backup okay", "backup_status"),
    ("what routines do I have", "list_routines"),
    ("who won the cricket yesterday", "web_search|web"),
    ("explain what a mutex is in one sentence", "reply"),
]


def fetch(url: str, timeout: float = 5.0) -> tuple[int, Any]:
    """(status, JSON or text) of a GET. Raises OSError when nothing answers."""
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - our own services on this machine
            raw = r.read()
            code = r.status
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:300].decode(errors="replace")
    try:
        return code, json.loads(raw)
    except ValueError:
        return code, raw[:300].decode(errors="replace")


def row(name: str, level: str, detail: str, fix: str = "") -> Check:
    return {"name": name, "level": level, "detail": detail, "fix": fix}


def ollama(cfg: Config, get: Callable = fetch) -> list[Check]:
    url = cfg.ollama.url.rstrip("/")
    try:
        code, tags = get(url + "/api/tags")
    except OSError as e:
        return [row("ollama", "bad", f"not answering at {url} ({str(e)[:60]})", "start Ollama (dev.ps1 up starts it)")]
    if code != 200 or not isinstance(tags, dict):
        return [row("ollama", "bad", f"answered {code}", "check Ollama's log")]
    have = {m.get("name", "").lower() for m in tags.get("models") or []}
    out = [row("ollama", "ok", f"{len(have)} models")]
    for tier, t in cfg.models.tiers.items():
        if t.provider == "ollama" and t.model and tier in cfg.models.chain:
            name = t.model.lower()
            if name not in have and f"{name}:latest" not in have:
                out.append(row(f"model {tier}", "bad", f"{t.model} isn't pulled", f"ollama pull {t.model}"))
    try:
        code, ps = get(url + "/api/ps")
        running = {m.get("name", "").lower(): m for m in (ps.get("models") or [])} if code == 200 else {}
        first = cfg.models.tiers.get(cfg.models.chain[0]) if cfg.models.chain else None
        if first is not None and first.provider == "ollama" and first.model:
            m = running.get(first.model.lower()) or running.get(f"{first.model.lower()}:latest")
            if m:
                size, vram = m.get("size") or 0, m.get("size_vram") or 0
                share = int(100 * vram / size) if size else 0
                level = "ok" if share >= 95 else "warn"
                out.append(row("model on the GPU", level, f"{first.model}: {share}% in GPU memory",
                               "" if level == "ok" else "part of it runs on the CPU (slow): close GPU-heavy apps or "
                               "lower ollama.num_ctx"))
            else:
                out.append(row("model on the GPU", "ok", f"{first.model} not loaded right now (loads on the next "
                                                          "question)"))
    except OSError:
        pass
    return out


def searxng(cfg: Config, get: Callable = fetch) -> Check:
    conf = cfg.plugins.config.get("web", {})
    base = str(conf.get("searx_url") or "http://127.0.0.1:8888").rstrip("/")
    live = "web" in cfg.plugins.live
    miss = "warn" if not live else "bad"
    try:
        code, body = get(base + "/search?q=test&format=json", timeout=10)
    except OSError:
        return row("web search", miss, f"SearXNG isn't answering at {base}",
                   "start it: cd deploy\\searxng; docker compose up -d (and match web.searx_url to its port)")
    if code == 403:
        return row("web search", miss, "SearXNG refuses JSON", "turn on the json format in deploy/searxng/settings.yml")
    if code != 200 or not isinstance(body, dict):
        return row("web search", miss, f"SearXNG answered {code}", "check its container log")
    n = len(body.get("results") or [])
    return row("web search", "ok" if n else "warn", f"SearXNG answers ({n} results for a test search)",
               "" if n else "its search engines may be blocked right now")


def voice(cfg: Config, get: Callable = fetch) -> Check:
    if cfg.ari.voice_engine != "expressive":
        return row("voice", "ok", "Piper (the expressive voice is off)")
    try:
        code, h = get(cfg.ari.expressive_url.rstrip("/") + "/health")
    except OSError:
        return row("voice", "bad", "the expressive voice isn't running (Piper is used)",
                   "dev.ps1 up starts ari-voice; see logs\\ari-voice-crash.log")
    if not isinstance(h, dict) or code != 200:
        return row("voice", "bad", f"answered {code}", "see logs\\ari-voice-crash.log")
    if not h.get("loaded"):
        return row("voice", "warn", "up, but its model isn't loaded yet (the first sentence will be slow)",
                   "wait a minute after starting; it warms itself")
    if h.get("device") != "cuda":
        return row("voice", "bad", f"running on the {h.get('device')}: every sentence will be slow",
                   "install the CUDA build of PyTorch in .venv-voice")
    return row("voice", "ok", f"{h.get('model')} on the GPU, loaded")


def hearing(cfg: Config) -> Check:
    if not (cfg.ari.listen or cfg.ari.hearing == "whisper"):
        return row("hearing", "ok", "off")
    try:
        import ctranslate2  # type: ignore[import-not-found]
    except ImportError:
        return row("hearing", "warn", "can't check here (faster-whisper isn't installed in this Python)",
                   "pip install -e .[hearing] in .venv")
    try:
        gpus = int(ctranslate2.get_cuda_device_count())
    except Exception:  # noqa: BLE001
        gpus = 0
    if not gpus:
        return row("hearing", "warn", "Whisper has no GPU: it runs on the CPU (slower, more power)",
                   "pip install -e .[hearing] in .venv (adds the CUDA libraries)")
    return row("hearing", "ok", f"Whisper can use the GPU ({gpus})")


def listener(cfg: Config, seen: float, now: float) -> Check | None:
    if not cfg.ari.listen:
        return None
    if now - seen < 90:
        return row("listener", "ok", "\"Hey Ari\" is listening on the PC")
    return row("listener", "warn", "no word from the PC's listener in the last minutes",
               "dev.ps1 up (logs\\ari-listen.out, logs\\ari.log)")


def routing(cfg: Config, tools: list[dict], decide: Callable | None = None) -> Check | None:
    """Does the local model still pick the right tool? (a short version of `python -m argus.ari_eval`)"""
    from .ari_eval import first_step, ollama_decider

    first = cfg.models.tiers.get(cfg.models.chain[0]) if cfg.models.chain else None
    if decide is None:
        if first is None or first.provider != "ollama" or not first.model:
            return None
        decide = ollama_decider(cfg.ollama.url, first.model, cfg.ollama.num_ctx)
    have = {t["name"] for t in tools}
    cases = [(s, e) for s, e in SMOKE if e == "reply" or any(x in have for x in e.split("|"))]
    if not cases:
        return None
    right, times, wrong = 0, [], []
    for say, expect in cases:
        info: dict = {}
        try:
            got, took = first_step(say, tools, decide, time.strftime("%A %d %B %Y, %H:%M"), info)
        except Exception as e:  # noqa: BLE001 - the model is down: the ollama check says so
            return row("tool picking", "bad", f"the model didn't answer ({str(e)[:60]})", "see the ollama check")
        if got in expect.split("|"):
            right += 1
        else:
            wrong.append(f"{say!r} -> {got}")
        if took:
            times.append(took)
    times.sort()
    p50 = times[len(times) // 2] if times else 0.0
    share = right / len(cases)
    level = "ok" if share >= 0.85 and p50 < 2.0 else "warn" if share >= 0.6 else "bad"
    detail = f"{right} of {len(cases)} right, a model step takes {p50:.1f} s"
    if wrong:
        detail += "; wrong: " + "; ".join(wrong[:3])
    fix = "" if level == "ok" else "run .\\.venv\\Scripts\\python -m argus.ari_eval: the misses and times"
    return row("tool picking", level, detail, fix)


def run(cfg: Config, *, tools: list[dict], listener_seen: float = 0.0, gpu_workers: int | None = None,
        get: Callable = fetch, decide: Callable | None = None, now: float | None = None) -> dict:
    """Every check, in order. {"at", "ok" (nothing bad), "problems" (bad or warn), "checks"}."""
    now = time.time() if now is None else now
    checks = ollama(cfg, get)
    checks.append(searxng(cfg, get))
    checks.append(voice(cfg, get))
    checks.append(hearing(cfg))
    if (c := listener(cfg, listener_seen, now)) is not None:
        checks.append(c)
    if gpu_workers is not None:
        checks.append(row("worker", "ok" if gpu_workers else "warn",
                          f"{gpu_workers} GPU worker(s) online" if gpu_workers else "no GPU worker online",
                          "" if gpu_workers else "dev.ps1 up starts the PC worker"))
    if all(c["level"] != "bad" for c in checks[:1]):  # the model check needs Ollama
        if (c := routing(cfg, tools, decide)) is not None:
            checks.append(c)
    bad = [c for c in checks if c["level"] == "bad"]
    problems = [c for c in checks if c["level"] != "ok"]
    return {"at": now, "ok": not bad, "problems": len(problems), "checks": checks}


def summary(result: dict) -> str:
    """One sentence for the phone: what is wrong, and the first thing to do."""
    bad = [c for c in result["checks"] if c["level"] != "ok"]
    if not bad:
        return "Everything Ari needs is working."
    first = bad[0]
    return (f"{len(bad)} thing(s) to look at: " + ", ".join(c["name"] for c in bad)
            + (f". First: {first['detail']}" + (f" ({first['fix']})" if first["fix"] else "") + "."))
