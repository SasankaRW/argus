"""Eyes clip maker: Claude makes a robot-eyes clip for each app you use, once, and the player reuses it.

`scan` (every 5 minutes) reads data/eyes/seen.json (seconds each app key has been in front, written by the player)
and picks the app with the most time that has earned a clip (`min_seconds`) and has none: no clips/<key>.eyes and
no entry in clips/map.json. One clip at a time, `clips_per_day` a day, two tries per key.

For that key a fresh studio folder is made (data/eyes/studio/<key>-<time>/) with the eye engine, clipkit, the
examples, sheet.py, render.py, STYLE.md and clips.md (the clips there are, with their one-line descriptions).
Claude Code runs in it with a short tool list: read, write and edit files there, and run only
`python sheet.py ...` and `python render.py ...`. No web, no other commands, no secrets.
It either reuses a clip (result.json {"reuse": name}: the key is added to map.json; never for `own_clip` keys) or
writes <key>.py, looks at contact sheets, fixes it and renders it (result.json {"clip": file, "about": line}).

Argus then puts fresh copies of the kit back, renders the clip itself and checks it (argus.eyes.check_clip: the
loop joins, it starts on the shared resting pose and comes home at least every 15 s, eyes only, 20-90 s). A clip
with problems goes back to Claude once with the list; then it is saved as clips/<key>.eyes (the player picks it up
within a second) or the key is left on `default`. The studio folder stays for a look (claude.log, sheets).

`make` (input: key, note) makes or remakes one key's clip now; `note` is your wish ("make it dance more").
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from argus.worker import Context, PermanentError, workflow

PLUGIN = "eyes"
KIT = Path(__file__).resolve().parent / "studio"
KIT_FILES = ("eyecore.py", "clipkit.py", "sheet.py", "render.py", "STYLE.md")
RESERVED = {"default", "new", "map", "seen", "rest"}
ALLOW = "Read,Glob,Grep,Write,Edit,MultiEdit,Bash(python sheet.py *),Bash(python render.py *)"
DENY = ("WebFetch,WebSearch,Task,NotebookEdit,Read(**/.env*),Read(**/*.pem),Read(**/*.key),Read(**/id_rsa*),"
        "Read(**/*secret*),Read(**/*credential*)," + ",".join(f"{t}(**/{f})" for t in ("Edit", "Write", "MultiEdit")
                                                         for f in KIT_FILES[:4]))
HINTS = {
    "youtube": "watching videos on YouTube",
    "github": "GitHub in the browser: code, pull requests, issues",
    "gmail": "reading and writing email in Gmail",
    "browser": "browsing the web (any site without its own clip)",
    "code": "coding in VS Code",
    "explorer": "the Windows file explorer: looking through folders and files",
    "windowsterminal": "the Windows Terminal: typing commands",
    "claude": "talking with Claude, an AI assistant",
    "spotify": "listening to music in Spotify",
    "discord": "chatting with friends on Discord",
    "notepad": "writing a quick note in Notepad",
    "steam": "browsing games on Steam",
}


# ---------------------------------------------------------------------- pure helpers (tested)


def hint(key: str) -> str:
    return HINTS.get(key, f"the program {key}.exe (guess what it is for from its name)")


def candidates(seen: dict, clips: Path, mapping: dict, tried: dict, min_s: float) -> list[str]:
    """Keys that have earned a clip and have none, most time in front first."""
    out = []
    for key, rec in (seen or {}).items():
        k = str(key)
        if not re.fullmatch(r"[a-z0-9_-]{1,40}", k) or k in RESERVED or k in mapping:
            continue
        if (clips / f"{k}.eyes").exists() or int(tried.get(k, 0)) >= 2:
            continue
        secs = float((rec or {}).get("seconds", 0)) if isinstance(rec, dict) else 0.0
        if secs >= min_s:
            out.append((secs, k))
    return [k for _, k in sorted(out, reverse=True)]


def norm_model(name: str) -> str:
    m = str(name or "").strip().lower()
    if m in ("opus", "sonnet", "haiku") or re.fullmatch(r"claude-[a-z0-9.\-]{3,40}", m):
        return m
    raise PermanentError(f"model {name!r}: use opus, sonnet, haiku or a full claude-... id")


def clips_list(clips: Path) -> list[tuple[str, str]]:
    """(name, about) of the saved clips (not `new`: that one is only for apps the eyes haven't met yet)."""
    from argus.eyes import Clip

    out = []
    for f in sorted(clips.glob("*.eyes")):
        if f.stem == "new":
            continue
        try:
            out.append((f.stem, Clip.load(f).about))
        except (OSError, ValueError):
            continue
    return out


def prompt(key: str, own: bool, clips: list[tuple[str, str]], note: str = "", problems: list[str] = ()) -> str:
    listing = "\n".join(f"- {n}: {a}" for n, a in clips) or "- (none yet)"
    if problems:
        return (f"You made `{key}.py` in this folder earlier for the app `{key}` ({hint(key)}), but Argus's checks "
                "found problems:\n" + "\n".join(f"- {p}" for p in problems) +
                f"\n\nFix `{key}.py` (read STYLE.md again if needed), run `python render.py {key}.py` until it says "
                f"\"all checks passed\", then write result.json again: {{\"clip\": \"{key}.py\", \"about\": \"...\"}}.")
    reuse = ("" if own else
             "First decide whether one of these existing clips already fits this app well (for example another code "
             "editor -> a code clip, another browser site -> browser). Never reuse `default`. If one fits, write "
             "result.json with {\"reuse\": \"<its name>\"} and stop there.\n\n")
    wish = f"\nThe user asked: {note.strip()[:400]}\n" if note.strip() else ""
    return (
        f"You are making a robot-eyes animation clip for the app `{key}`: {hint(key)}. It plays on the small "
        "screen of the user's PC cooler whenever this app is in front.\n\n"
        f"The clips there are now (clips.md):\n{listing}\n\n{reuse}"
        f"Otherwise make a new clip: read STYLE.md, clipkit.py and the two examples in examples/, then write "
        f"`{key}.py` in this folder. Look at it with `python sheet.py {key}.py` (Read sheet.png), improve it, then run "
        f"`python render.py {key}.py` until it says \"all checks passed\".{wish}\n"
        f"Finally write result.json: {{\"clip\": \"{key}.py\", \"about\": \"<the ABOUT line>\"}}.\n"
        "Only work in this folder. The only commands you can run are `python sheet.py ...` and `python render.py ...`."
    )


def parse_result(text: str, key: str, names: set[str], own: bool) -> dict:
    """result.json -> {"reuse": name} or {"clip": file, "about": line}. PermanentError when it isn't usable."""
    try:
        r = json.loads(text)
    except ValueError:
        raise PermanentError("result.json isn't JSON") from None
    if not isinstance(r, dict):
        raise PermanentError("result.json isn't an object")
    if r.get("reuse"):
        name = str(r["reuse"]).strip().removesuffix(".eyes")
        if own:
            raise PermanentError(f"{key} must get its own clip, not reuse {name}")
        if name in ("default", "new") or name not in names:
            raise PermanentError(f"reuse {name!r}: no such clip to reuse")
        return {"reuse": name}
    clip = str(r.get("clip") or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}\.py", clip) or Path(clip).stem in ("eyecore", "clipkit", "sheet",
                                                                                  "render"):
        raise PermanentError(f"result.json: clip {clip!r} isn't a clip file in the studio folder")
    about = re.sub(r"\s+", " ", str(r.get("about") or "")).strip()[:160] or key
    return {"clip": clip, "about": about}


def day_count(store_runs: dict) -> int:
    return int(store_runs.get("n", 0)) if store_runs.get("day") == time.strftime("%Y-%m-%d") else 0


def write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path) -> dict:
    try:
        got = json.loads(path.read_text(encoding="utf-8"))
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


# ---------------------------------------------------------------------- the PC (tests replace run_claude/render)


def clips_dir(ctx: Context) -> Path:
    from argus.config import load_config
    from argus.eyes import clips_dir as where

    return where(load_config())


def env() -> dict:
    """The worker's own Python first on PATH, so `python` in Claude's shell is the one with argus, numpy and
    Pillow."""
    e = dict(os.environ)
    import argus

    e["PATH"] = str(Path(sys.executable).parent) + os.pathsep + e.get("PATH", "")
    core = str(Path(argus.__file__).resolve().parents[1])  # argus itself, installed or not
    e["PYTHONPATH"] = core + (os.pathsep + e["PYTHONPATH"] if e.get("PYTHONPATH") else "")
    e.pop("ANTHROPIC_API_KEY", None)  # your Claude Code login, as for the ticket fixer
    return e


def run_claude(command: str, model: str, text: str, cwd: Path, timeout: float) -> str:
    exe = shutil.which(command) or command
    args = [exe, "-p", "--model", model, "--output-format", "json", "--permission-mode", "acceptEdits",
            "--allowedTools", ALLOW, "--disallowedTools", DENY, "--max-turns", "60", "--no-session-persistence"]
    try:
        r = subprocess.run(args, input=text, cwd=cwd, env=env(), capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except FileNotFoundError:
        raise PermanentError("the claude command wasn't found: install Claude Code, or set claude_command") from None
    except subprocess.TimeoutExpired:
        raise PermanentError(f"Claude took longer than {int(timeout // 60)} minutes and was stopped") from None
    return (r.stdout or "") + ("\n--- stderr ---\n" + r.stderr if r.stderr else "")


def render(work: Path, clip: str, timeout: float = 600) -> bytes:
    """Argus renders the clip itself, with fresh copies of the kit (whatever Claude did to them)."""
    for f in KIT_FILES:
        shutil.copy2(KIT / f, work / f)
    out = work / "out" / (Path(clip).stem + ".eyes")
    out.unlink(missing_ok=True)
    try:
        r = subprocess.run([sys.executable, "render.py", clip], cwd=work, env=env(), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        raise PermanentError("rendering took too long") from None
    if not out.exists():
        raise PermanentError("render failed: " + ((r.stderr or r.stdout or "").strip()[-400:] or "no output"))
    return out.read_bytes()


# ---------------------------------------------------------------------- one clip


def setup(clips: Path, key: str) -> Path:
    from argus.eyes import Clip

    base = clips.parent / "studio" / f"{key}-{time.strftime('%Y%m%d-%H%M%S')}"
    work, n = base, 1
    while work.exists():
        n += 1
        work = base.with_name(f"{base.name}-{n}")
    (work / "examples").mkdir(parents=True)
    for f in KIT_FILES:
        shutil.copy2(KIT / f, work / f)
    for f in (KIT / "examples").glob("*.py"):
        shutil.copy2(f, work / "examples" / f.name)
    rest = clips / "default.eyes"
    if not rest.exists():
        raise PermanentError(f"{rest} is missing: it holds the shared resting pose every clip starts from")
    (work / "rest.frame").write_bytes(Clip.load(rest).pixels(0))
    (work / "clips.md").write_text("".join(f"- {n}: {a}\n" for n, a in clips_list(clips)), encoding="utf-8")
    return work


def make_clip(ctx: Context, clips: Path, key: str, note: str = "") -> dict:
    from argus.eyes import check_clip, repack

    own = key in {str(k).lower() for k in ctx.config.get("own_clip") or []}
    model = norm_model(str(ctx.config.get("model") or "opus"))
    command = str(ctx.config.get("claude_command") or "claude")
    timeout = float(ctx.config.get("minutes") or 15) * 60
    work = setup(clips, key)
    names = {n for n, _ in clips_list(clips)}
    rest = (work / "rest.frame").read_bytes()
    problems: list[str] = []
    started = time.monotonic()
    for attempt in (1, 2):
        text = prompt(key, own, sorted(clips_list(clips)), note, problems)
        (work / "result.json").unlink(missing_ok=True)
        log = run_claude(command, model, text, work, timeout)
        (work / f"claude-{attempt}.log").write_text(str(log or ""), encoding="utf-8")
        res_file = work / "result.json"
        if not res_file.exists():
            raise PermanentError("Claude finished without writing result.json (see claude.log in " + str(work) + ")")
        res = parse_result(res_file.read_text(encoding="utf-8"), key, names, own)
        if "reuse" in res:
            mapping = read_json(clips / "map.json")
            mapping[key] = res["reuse"]
            write_json(clips / "map.json", mapping)
            return {"key": key, "reused": res["reuse"], "studio": str(work),
                    "minutes": round((time.monotonic() - started) / 60, 1)}
        data = render(work, res["clip"])
        chk = check_clip(data, rest)
        problems = chk["problems"]
        if not problems:
            final = repack(data, chk["neutral"], res["about"])
            tmp = clips / f"{key}.eyes.tmp"
            tmp.write_bytes(final)
            tmp.replace(clips / f"{key}.eyes")
            return {"key": key, "clip": f"{key}.eyes", "about": res["about"], "seconds": chk["seconds"],
                    "attempts": attempt, "studio": str(work), "minutes": round((time.monotonic() - started) / 60, 1)}
        ctx.log("clip failed the checks", key=key, attempt=attempt, problems=problems)
    raise PermanentError(f"the {key} clip still fails the checks: " + "; ".join(problems)[:300])


def record(ctx: Context, key: str, outcome: dict) -> None:
    runs = ctx.store.get("runs") or {}
    today = time.strftime("%Y-%m-%d")
    ctx.store.set("runs", {"day": today, "n": day_count(runs) + 1})
    tried = ctx.store.get("tried") or {}
    if "failed" in outcome:
        tried[key] = int(tried.get(key, 0)) + 1
        ctx.store.set("tried", tried)
    history = (ctx.store.get("history") or [])[-49:]
    ctx.store.set("history", history + [{"at": time.strftime("%Y-%m-%d %H:%M"), **outcome}])


def run_one(ctx: Context, clips: Path, key: str, note: str = "") -> dict:
    ctx.store.set("busy", {"key": key, "at": time.time()})
    try:
        out = make_clip(ctx, clips, key, note)
    except PermanentError as e:
        out = {"key": key, "failed": str(e)[:400]}
    finally:
        ctx.store.set("busy", {})
    record(ctx, key, out)
    ctx.log("eyes clip", **{k: v for k, v in out.items() if k != "studio"})
    return out


def busy(ctx: Context) -> str:
    b = ctx.store.get("busy") or {}
    window = float(ctx.config.get("minutes") or 15) * 60 * 2 + 600
    return str(b.get("key") or "") if b.get("key") and time.time() - float(b.get("at", 0)) < window else ""


def limit_left(ctx: Context) -> int:
    return int(ctx.config.get("clips_per_day") or 6) - day_count(ctx.store.get("runs") or {})


@workflow(PLUGIN, "scan")
def scan(ctx: Context):
    def pick() -> dict:
        if busy(ctx):
            return {"busy": busy(ctx)}
        clips = clips_dir(ctx)
        keys = candidates(read_json(clips.parent / "seen.json"), clips, read_json(clips / "map.json"),
                          ctx.store.get("tried") or {}, float(ctx.config.get("min_seconds") or 120))
        if not keys:
            return {}
        if limit_left(ctx) <= 0:
            return {"waiting": f"already {ctx.config.get('clips_per_day') or 6} clips today", "key": keys[0]}
        return {"key": keys[0]}

    chosen = ctx.step("pick", pick)
    if not chosen.get("key") or chosen.get("waiting"):
        return chosen or {"made": None}
    if ctx.dry_run:
        return {"would_make": chosen["key"], "dry_run": True}
    return run_one(ctx, clips_dir(ctx), chosen["key"])


@workflow(PLUGIN, "make")
def make(ctx: Context):
    key = str(ctx.input.get("key") or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9_-]{1,40}", key) or key in RESERVED:
        raise PermanentError(f"key {key!r}: an app key like code, spotify, youtube")
    if busy(ctx):
        raise PermanentError(f"a clip for {busy(ctx)} is being made: try again in a few minutes")
    if ctx.dry_run:
        return {"would_make": key, "dry_run": True}
    clips = clips_dir(ctx)
    mapping = read_json(clips / "map.json")
    if key in mapping:  # remaking: its own clip from now on
        mapping.pop(key)
        write_json(clips / "map.json", mapping)
    return run_one(ctx, clips, key, str(ctx.input.get("note") or ""))


@workflow(PLUGIN, "status")
def status(ctx: Context):
    def go() -> dict:
        clips = clips_dir(ctx)
        seen = read_json(clips.parent / "seen.json")
        return {"clips": [{"name": n, "about": a} for n, a in clips_list(clips)],
                "map": read_json(clips / "map.json"),
                "waiting": candidates(seen, clips, read_json(clips / "map.json"), ctx.store.get("tried") or {},
                                      float(ctx.config.get("min_seconds") or 120)),
                "today": day_count(ctx.store.get("runs") or {}), "limit": int(ctx.config.get("clips_per_day") or 6),
                "busy": busy(ctx), "history": (ctx.store.get("history") or [])[-10:]}

    return ctx.step("status", go)
