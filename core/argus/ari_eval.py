"""Ari's test set: does it pick the right tool, and how fast? Nothing is done for real (no tool runs).

    python -m argus.ari_eval              (Argus running: it lists Ari's tools; the local model decides)
    python -m argus.ari_eval --cases my-cases.yaml
    python -m argus.ari_eval --conversations    (scripted conversations: does Ari keep the thread? see
                                                 argus/ari_conversations.py; the score shows in Helios)

Each case is something you'd say and what should happen: a tool's name, "chat" (small talk), "reply" (answered
from what the model knows) or "web" (needs the internet). Several are allowed: "weather|web_search". The same
gates as a real message run first (small talk, the direct paths like "volume 30"), then one model step, as Ari's
first decision. The result is printed and kept in data/ari-eval/ (one file per run), so a change can be compared
with the run before it.

Ari's own picks you marked Correct in Helios (GET /guidance/ari-cases) are added as cases too (`--no-learned`
leaves them out), so what Ari learned stays tested.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

CASES: list[tuple[str, str]] = [
    ("open brave", "open_app"),
    ("volume 30", "set_volume"),
    ("turn the volume down a bit", "set_volume"),
    ("next song", "media_control"),
    ("turn on the torch", "phone_torch"),
    ("start a timer for 10 minutes on my phone", "phone_timer"),
    ("where is my phone", "where_is_my_phone|ring_phone|ring_phone_now|phone_status"),
    ("turn down my volume on my phone", "phone_volume"),
    ("what's on my screen", "look_at_screen"),
    ("sum up what I copied", "summarise_clipboard"),
    ("type hello world", "type_text"),
    ("how are you", "chat"),
    ("I'm so tired", "chat"),
    ("tell me a joke", "chat"),
    ("good morning", "chat"),
    ("never mind, thank you", "chat"),
    ("just wanna talk with you, I'm tired", "chat"),
    ("what's the weather like", "weather"),
    ("will it rain in Kandy tomorrow", "weather|web_search|web"),
    ("find my CV", "search_everything|find_file|search_my_files"),
    ("what did I note about the server", "find_notes|search_my_files"),
    ("note that the router password changed", "add_note|remember"),
    ("is the laptop server up", "lab_status"),
    ("what's running right now", "argus_status|list_jobs"),
    ("text Kaancha I'm running late on WhatsApp", "whatsapp_message"),
    ("any changes in my repos today", "what_changed_today|repo_status"),
    ("add milk to the shopping list", "add_to_list|shopping_list"),
    ("how much did I spend this month", "money_this_month"),
    ("who won the cricket yesterday", "web_search|web"),
    ("latest python release", "web_search|web"),
    ("can you search for cat pictures", "web_search|open_website|web"),
    ("what's 15 percent of 2400", "reply"),
    ("explain what a mutex is in one sentence", "reply"),
    ("close spotify", "close_app"),
    ("lock my pc", "lock_pc"),
    ("show me my open issues", "list_issues|work_summary"),
    ("save this for later https://example.com/post", "save_for_later"),
    ("how was my week", "weekly_review|time_saved"),
    ("is the backup okay", "backup_status"),
    ("what routines do I have", "list_routines"),
    ("move that window to me", "move_window_to_me"),
    ("take this", "take_window"),
    ("google cheap flights to Bangkok", "search_in_browser"),
]


def load_cases(path: Path | None) -> list[tuple[str, str]]:
    if path is None:
        return CASES
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    return [(str(c["say"]), str(c["expect"])) for c in data]


def first_step(text: str, tools: list[dict], decide: Any, now: str = "", info: dict | None = None) -> tuple[str, float]:
    """What Ari would do first with `text`: ("chat" | a tool name | "reply" | "web", seconds the model took).
    `info` gets how many tools were offered and what the model reported (prompt/output tokens, load and prompt time)."""
    got, _args, took = first_pick(text, tools, decide, now, info)
    return got, took


def first_pick(text: str, tools: list[dict], decide: Any, now: str = "", info: dict | None = None,
               history: list[dict] | None = None, thread: dict | None = None) -> tuple[str, dict, float]:
    """first_step with the tool's arguments, in a conversation: `history` the turns so far and `thread` where it
    stands (as argusd would hand them to Ari)."""
    from . import thread as thread_mod
    from .ari import weather_ask
    from .worker.think import PHONE_VOLUME, chatty, follow_on, offered, quick_math, situation, straight_to

    if quick_math(text) or (PHONE_VOLUME.search(text) and "phone_volume" not in {t["name"] for t in tools}):
        return "reply", {}, 0.0
    if chatty(text):
        return "chat", {}, 0.0
    wx = weather_ask(text)
    if wx is not None and any(t["name"] == "weather" for t in tools):  # argusd answers these itself, no model
        return "weather", {**({"place": wx[0]} if wx[0] else {}), **({"tomorrow": True} if wx[1] else {})}, 0.0
    brief = thread_mod.brief(thread) if thread else {}
    have = offered({t["name"]: t for t in tools}, text, history or [], thread_mod.words(thread) if brief else "")
    direct = straight_to(have, text) or follow_on(have, text, thread)
    if direct is not None:
        return direct[0], direct[1], 0.0
    t0 = time.perf_counter()
    step = decide({"message": text, **({"thread": brief} if brief else {}),
                   "conversation_so_far": (history or [])[-8:], "you_remember": [], "now": now,
                   "situation": situation(text, None), "tools": list(have.values())})
    took = time.perf_counter() - t0
    if info is not None:
        m = step.get("_meta") or {}
        info.update({"tools": len(have), "prompt_tok": m.get("prompt_eval_count"), "out_tok": m.get("eval_count"),
                     "load_s": round((m.get("load_duration") or 0) / 1e9, 2),
                     "prompt_s": round((m.get("prompt_eval_duration") or 0) / 1e9, 2),
                     "gen_s": round((m.get("eval_duration") or 0) / 1e9, 2)})
    if step.get("tool"):
        return str(step["tool"]), step.get("args") if isinstance(step.get("args"), dict) else {}, took
    return ("web" if step.get("need_web") else "reply"), {}, took


def ollama_decider(url: str, model: str, num_ctx: int = 8192) -> Any:
    from .models.providers import OllamaProvider
    from .models.router import parse_json
    from .worker.think import PERSONA, PLAYBOOK, Step

    p = OllamaProvider(url, model, keep_alive="10m", num_ctx=num_ctx)
    system = (PLAYBOOK + "\n\n" + PERSONA + "\n\nReply with only a JSON object that matches the given schema. "
              "No extra text.")
    schema = Step.model_json_schema()

    def decide(task: dict) -> dict:
        r = p.chat(system, [{"role": "user", "content": json.dumps(task, ensure_ascii=False)}], schema)
        try:
            out = dict(parse_json(r.text))
        except ValueError:
            out = {}
        out["_meta"] = r.meta
        return out

    return decide


def run(cases: list[tuple[str, str]], tools: list[dict], decide: Any, log=print) -> dict:
    rows, times = [], []
    for say, expect in cases:
        info: dict = {}
        got, took = first_step(say, tools, decide, time.strftime("%A %d %B %Y, %H:%M"), info)
        ok = got in expect.split("|")
        if took:
            times.append(took)
        rows.append({"say": say, "expect": expect, "got": got, "ok": ok, "seconds": round(took, 2), **info})
        more = (f"  [{info['tools']} tools, prompt {info['prompt_tok']} tok in {info['prompt_s']} s, "
                f"out {info['out_tok']} tok in {info['gen_s']} s, load {info['load_s']} s]") if info else ""
        miss = "" if ok else f"  (wanted {expect})"
        log(f"{'ok  ' if ok else 'MISS'} {took:5.2f}s  {say!r:52} -> {got}{miss}{more}")
    times.sort()
    out = {"at": time.strftime("%Y-%m-%d %H:%M"), "right": sum(r["ok"] for r in rows), "of": len(rows),
           "p50_s": round(times[len(times) // 2], 2) if times else 0.0,
           "p90_s": round(times[min(len(times) - 1, int(len(times) * 0.9))], 2) if times else 0.0,
           "cases": rows}
    log(f"\n{out['right']}/{out['of']} right ({100 * out['right'] // max(1, out['of'])}%), "
        f"model step p50 {out['p50_s']} s, p90 {out['p90_s']} s")
    return out


PRIVATE = {"check_email", "read_email", "money_this_month", "look_at_screen", "summarise_clipboard"}
NOT_TOOLS = ("reply", "chat", "web")


def _has(want: str, got: Any) -> bool:
    """Does the argument hold one of the words wanted ("2|second")? Booleans are "true" / "false"."""
    if got is None or got == "":
        return False
    s = (str(got).lower() if not isinstance(got, bool) else ("true" if got else "false"))
    return any(w.strip().lower() in s for w in want.split("|"))


def conversation_can_run(conv: dict, names: set[str]) -> bool:
    """Every step's tool is one Ari has here (a conversation about a plugin you don't run is left out)."""
    return all(any(x in NOT_TOOLS or x in names for x in st["expect"].split("|")) for st in conv["steps"])


def run_conversations(convs: list[dict], tools: list[dict], decide: Any, log=print) -> dict:
    """Each conversation step by step, with the history and thread the scripted turns before it leave behind."""
    from . import thread as thread_mod

    names = {t["name"] for t in tools}
    rows, times, skipped = [], [], []
    for conv in convs:
        if not conversation_can_run(conv, names):
            skipped.append(conv["name"])
            continue
        history: list[dict] = []
        thread: dict | None = None
        steps = []
        for st in conv["steps"]:
            info: dict = {}
            got, args, took = first_pick(st["say"], tools, decide, time.strftime("%A %d %B %Y, %H:%M"), info,
                                         history, thread)
            tool_ok = got in st["expect"].split("|")
            bad = [k for k, v in st["args"].items() if not _has(v, args.get(k))] if tool_ok and \
                got not in NOT_TOOLS else []
            ok = tool_ok and not bad
            if took:
                times.append(took)
            steps.append({"say": st["say"], "expect": st["expect"], "want_args": st["args"], "got": got,
                          "args": args, "ok": ok, "seconds": round(took, 2), **({"wrong_args": bad} if bad else {})})
            # what "happened" (the script, not the model): the next step's context
            first = st["expect"].split("|")[0]
            used = [] if first in NOT_TOOLS else [{"tool": first, "args": st["do"], "result": st["result"],
                                                   **({"private": True} if first in PRIVATE else {})}]
            history = [{k: v for k, v in h.items() if k != "found"} for h in history]  # only the last carries it
            history.append({"role": "you", "text": st["say"]})
            ari: dict[str, Any] = {"role": "ari", "text": st["said"]}
            if used and used[0].get("private"):
                ari["private"] = True
            elif used:
                found = [{k: u[k] for k in ("tool", "args", "result") if u.get(k) is not None} for u in used]
                s = json.dumps(found, ensure_ascii=False, default=str)
                ari["found"] = found if len(s) <= 1200 else s[:1200] + "…"
            history.append(ari)
            pending = {"kind": "tool", "name": first, "args": st["do"]} if st["asks"] else None
            thread = thread_mod.update(thread, message=st["say"],
                                       out={"reply": st["said"], "used": used, "pending": pending})
        right = all(x["ok"] for x in steps)
        rows.append({"name": conv["name"], "ok": right, "steps": steps})
        log(f"{'ok  ' if right else 'MISS'} {conv['name']}")
        for x in steps:
            if not x["ok"]:
                why = f"args {x['args']}, wanted {x['want_args']}" if x.get("wrong_args") else f"wanted {x['expect']}"
                log(f"       {x['say']!r:40} -> {x['got']} ({why})")
    times.sort()
    n_steps = sum(len(r["steps"]) for r in rows)
    out = {"at": time.strftime("%Y-%m-%d %H:%M"), "right": sum(r["ok"] for r in rows), "of": len(rows),
           "steps_right": sum(x["ok"] for r in rows for x in r["steps"]), "steps": n_steps,
           "p50_s": round(times[len(times) // 2], 2) if times else 0.0, "skipped": skipped, "conversations": rows}
    log(f"\n{out['right']}/{out['of']} conversations right, {out['steps_right']}/{n_steps} steps"
        + (f" ({len(skipped)} left out: their tools aren't here)" if skipped else ""))
    return out


def summary(out: dict, model: str) -> dict:
    """What argusd keeps of a conversation run (for Helios): the score and the misses, not every step."""
    misses = [{"name": r["name"], "say": x["say"], "got": x["got"], "expect": x["expect"]}
              for r in out["conversations"] for x in r["steps"] if not x["ok"]]
    return {"at": time.time(), "model": model, "right": out["right"], "of": out["of"],
            "steps_right": out["steps_right"], "steps": out["steps"], "p50": out["p50_s"], "misses": misses[:20]}


def main(argv: list[str] | None = None) -> int:
    from .config import load_config, parse_env_file

    ap = argparse.ArgumentParser(prog="ari-eval", description="Does Ari pick the right tool? (nothing runs)")
    ap.add_argument("--url", default=os.environ.get("ARGUS_URL", "http://127.0.0.1:8600"))
    ap.add_argument("--cases", type=Path, default=None, help="a YAML list of {say, expect}")
    ap.add_argument("--model", default=None, help="the Ollama model (default: the first tier's)")
    ap.add_argument("--no-learned", action="store_true", help="leave out the picks you marked Correct in Helios")
    ap.add_argument("--conversations", action="store_true",
                    help="the scripted conversations instead (does Ari keep the thread?); the score goes to Helios")
    a = ap.parse_args(argv)
    cfg = load_config()
    tier = cfg.models.tiers.get(cfg.models.chain[0]) if cfg.models.chain else None
    model = a.model or (tier.model if tier and tier.provider == "ollama" else None)
    if not model:
        print("No local model: set --model (an Ollama model name).", file=sys.stderr)
        return 2
    import urllib.request

    token = os.environ.get("ARGUS_WORKER_TOKEN") or parse_env_file(Path(".env")).get("ARGUS_WORKER_TOKEN")
    def get(path: str, body: Any = None) -> Any:
        head = {"Authorization": f"Bearer {token}"} if token else {}
        data = None
        if body is not None:
            data, head = json.dumps(body).encode(), {**head, "Content-Type": "application/json"}
        req = urllib.request.Request(a.url.rstrip("/") + path, data=data, headers=head)
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 - our own Argus
            return json.loads(r.read())

    try:
        tools = get("/tools")
    except OSError as e:
        print(f"Argus isn't answering at {a.url} ({e}): start it first (dev.ps1 up).", file=sys.stderr)
        return 2
    keep = cfg.db_path.parent / "ari-eval"
    keep.mkdir(parents=True, exist_ok=True)
    if a.conversations:
        from .ari_conversations import CONVERSATIONS

        print(f"{len(tools)} tools, {len(CONVERSATIONS)} conversations, model {model}\n")
        out = run_conversations(CONVERSATIONS, tools, ollama_decider(cfg.ollama.url, model, cfg.ollama.num_ctx))
        out["model"] = model
        (keep / f"conv-{time.strftime('%Y%m%d-%H%M%S')}.json").write_text(json.dumps(out, indent=1),
                                                                           encoding="utf-8")
        try:
            get("/ari/conversations", summary(out, model))
        except OSError as e:
            print(f"(couldn't send the score to Argus: {e})", file=sys.stderr)
        return 0
    cases = load_cases(a.cases)
    if not a.no_learned:
        try:
            have = {say for say, _ in cases}
            cases = cases + [(c["say"], c["expect"]) for c in get("/guidance/ari-cases") if c["say"] not in have]
        except (OSError, ValueError, KeyError):
            pass  # an older Argus: just the built-in cases
    print(f"{len(tools)} tools, {len(cases)} cases, model {model}\n")
    out = run(cases, tools, ollama_decider(cfg.ollama.url, model, cfg.ollama.num_ctx))
    out["model"] = model
    before = sorted(p for p in keep.glob("*.json") if not p.name.startswith("conv-"))
    (keep / f"{time.strftime('%Y%m%d-%H%M%S')}.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    if before:
        last = json.loads(before[-1].read_text(encoding="utf-8"))
        print(f"last run ({last.get('at')}): {last.get('right')}/{last.get('of')} right, p50 {last.get('p50_s')} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
