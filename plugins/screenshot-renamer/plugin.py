"""Screenshot renamer: "Screenshot 2026-09-28 214501.png" -> "2026-09-28 cashly login bug.png", in the same folder.

1. Read the text in the picture (Tesseract, when it is installed). Enough text -> T1 names it (T2 if T1's name
   fails the check).
2. Little or no text (a photo, a diagram), or no Tesseract -> the vision model (V1) looks at the picture itself.
3. Nothing usable -> the name stays as it is.

Only default names are touched (Screenshot..., Screen Shot..., Capture..., image...), never ones you gave. Renames
never overwrite, and each one can be undone from the job's Changes in Helios.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from argus.models import EscalationExhausted
from argus.worker import Context, PermanentError, workflow

PLUGIN = "screenshot-renamer"
DEFAULT_NAME = re.compile(r"^(screenshot|screen shot|capture|image|snip|scr)[\s_\-(]*[\d\s_\-.():]*(\(\d+\))?$", re.I)
DATE_IN_NAME = re.compile(r"(20\d{2})[-_.]?(\d{2})[-_.]?(\d{2})")
IMAGE_EXT = {".png", ".jpg", ".jpeg"}
MIN_TEXT = 25  # characters of OCR text worth handing to a text model
GENERIC = {"screenshot", "screen", "image", "picture", "capture", "photo", "untitled", "window", "desktop"}
FILLER = {"a", "an", "the", "of", "on", "with", "and", "for", "to", "some", "my"}
BAD_WIN_CHARS = r'[<>:"/\\|?*\x00-\x1f]'


def is_default_name(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in IMAGE_EXT and bool(DEFAULT_NAME.match(os.path.splitext(name)[0]))


def date_for(name: str, mtime: float) -> str:
    m = DATE_IN_NAME.search(name)
    if m:
        try:
            return datetime(int(m[1]), int(m[2]), int(m[3])).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")


def clean(words: str) -> str:
    """Lowercase words and digits only, single spaces, safe on Windows."""
    w = re.sub(BAD_WIN_CHARS, " ", words.lower())
    w = re.sub(r"[^a-z0-9 +#.-]+", " ", w)
    w = re.sub(r"\s+", " ", w).strip(" .-")
    return w


def problem(words: str) -> str | None:
    """Why a suggested name is not good enough, or None."""
    w = clean(words)
    n = len(w.split())
    if n < 3 or n > 6:
        return f"use 3 to 6 words, not {n}"
    if set(w.split()) <= GENERIC | FILLER:
        return "too generic: say what is on the screen"
    if DATE_IN_NAME.search(w) or re.search(r"\b\d{1,2}:\d{2}\b", w):
        return "leave dates and times out (the date is added in code)"
    return None


# ------------------------------------------------------------------ reading the picture

def _tesseract():
    """pytesseract + Pillow, ready to use, or None."""
    try:
        import pytesseract  # type: ignore[import-not-found]
        from PIL import Image  # type: ignore[import-not-found]
    except ImportError:
        return None
    win = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    if not shutil.which("tesseract"):
        if not os.path.exists(win):
            return None
        pytesseract.pytesseract.tesseract_cmd = win
    return pytesseract, Image


def read_screen(data: bytes) -> dict:
    """The text on screen, as {"text": all of it, "focus": the lines that matter most}.

    A screenshot usually shows one window in front and bits of others around it (a sidebar, the taskbar, another
    app). Lines are ranked by how big they are and how close to the middle, and lines hugging the left or right
    edge count much less, so "focus" is mostly the window you took the picture of. {} without Tesseract."""
    t = _tesseract()
    if t is None:
        return {}
    pytesseract, Image = t
    try:
        img = Image.open(io.BytesIO(data))
        d = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT, timeout=30)
    except Exception:  # a broken image or a Tesseract error: the vision model looks instead
        return {}
    return rank_lines(d, *img.size)


def rank_lines(d: dict, width: int, height: int) -> dict:
    words: dict[tuple, list[tuple[int, int, int, int, str]]] = {}
    for i, word in enumerate(d["text"]):
        word = str(word).strip()
        if not word or float(d["conf"][i]) < 50:
            continue
        key = (d["block_num"][i], d["par_num"][i], d["line_num"][i])
        words.setdefault(key, []).append((d["left"][i], d["top"][i], d["width"][i], d["height"][i], word))
    # Tesseract may join text across two windows into one line; a wide gap means a new piece.
    pieces: list[list[tuple[int, int, int, int, str]]] = []
    for ws in words.values():
        ws.sort()
        cur = [ws[0]]
        for w in ws[1:]:
            if w[0] - (cur[-1][0] + cur[-1][2]) > 0.035 * width:
                pieces.append(cur)
                cur = []
            cur.append(w)
        pieces.append(cur)
    scored = []
    for pc in pieces:
        text = " ".join(w[4] for w in pc)
        if len(re.sub(r"[^A-Za-z]", "", text)) < 3:
            continue
        left, right = min(w[0] for w in pc), max(w[0] + w[2] for w in pc)
        top, bottom = min(w[1] for w in pc), max(w[1] + w[3] for w in pc)
        cx, cy = (left + right) / 2 / width, (top + bottom) / 2 / height
        size = (bottom - top) / height
        centre = 1 - min(1.0, ((cx - 0.5) ** 2 + (cy - 0.5) ** 2) ** 0.5 / 0.71)
        edge = 0.2 if right / width < 0.14 or left / width > 0.86 else 1.0  # side panels of other windows
        scored.append(((size * 40 + centre) * edge * (1 + min(len(text), 40) / 80), text))
    scored.sort(key=lambda x: -x[0])
    return {"text": " ".join(t for _, t in scored), "focus": [t for _, t in scored[:20]]}


# Words a name may use even when they are not on the screen: what kind of thing is shown.
DESCRIBE = {"app", "page", "site", "window", "folder", "files", "file", "list", "table", "chart", "graph", "map",
            "dashboard", "settings", "dialog", "error", "bug", "warning", "login", "sign", "in", "form", "chat",
            "conversation", "message", "email", "code", "terminal", "console", "log", "logs", "editor", "browser",
            "explorer", "file-explorer", "view", "menu", "search", "results", "diagram", "photo", "receipt", "bill",
            "invoice", "statement", "report", "document", "pdf", "slide", "video", "game", "desktop", "notes",
            "review", "diff", "pull", "request", "pr", "issue", "tab", "status", "config", "install", "update",
            "download", "downloads", "upload", "home", "profile", "account", "calendar", "screenshot", "the", "a",
            "of", "on", "with", "and", "for", "to", "my", "new"}


def ungrounded(words: str, screen_text: str) -> list[str]:
    """Words of a suggested name that are neither on the screen nor plain description: made up."""
    seen = set(re.findall(r"[a-z0-9]+", screen_text.lower()))
    joined = " ".join(seen)
    out = []
    for w in clean(words).split():
        if w in DESCRIBE or w in seen or (len(w) >= 4 and w in joined):
            continue
        out.append(w)
    return out


def shrink(data: bytes, max_side: int) -> bytes:
    """A smaller PNG for the vision model (a 4K screenshot is slow and no better). Unchanged without Pillow."""
    try:
        from PIL import Image  # type: ignore[import-not-found]
    except ImportError:
        return data
    try:
        img = Image.open(io.BytesIO(data))
        if max(img.size) <= max_side:
            return data
        img.thumbnail((max_side, max_side))
        out = io.BytesIO()
        img.convert("RGB").save(out, format="PNG")
        return out.getvalue()
    except Exception:
        return data


# ------------------------------------------------------------------ naming

class Name(BaseModel):
    main_window: str = Field("", description="the app or site of the window in front, e.g. File Explorer")
    words: str = Field(description="3 to 6 lowercase words saying what that window shows")


PLAYBOOK = """You name screenshots so they are easy to find later.

First decide which window the screenshot is about: the one in front, usually the biggest and in the middle.
Everything else is background: other windows peeking out, sidebars of other apps, the taskbar, notifications.
Name only the window in front: its app or site, and what it shows (an error, a folder, a page, a chart, a chat).

Rules:
- Use words you can see in that window, or plain words for what it is (folder, error, login page, chart).
- Never take words from background windows or sidebars of other apps.
- Never guess project or product names that are not in the front window.
- Not sure what it shows? Name the app and the kind of screen: "file explorer downloads folder".

Good: "file explorer downloads folder", "cashly login bug", "github pr review argus", "vscode python traceback".
3 to 6 lowercase words. No dates, no times, no file extension, no punctuation.
Answer as JSON: {"main_window": "<app or site in front>", "words": "<3 to 6 words>"}"""


def suggest(ctx: Context, path: str, data: bytes) -> tuple[str | None, str]:
    """-> (words, how). The vision model first (it sees which window is in front); the text on screen checks it
    and is the fallback when there is no vision model."""
    screen = read_screen(data)
    text = screen.get("text", "")
    focus = " ".join(screen.get("focus", []))  # names must come from the window in front, not the background

    def check(n: Name, _inp) -> str | None:
        p = problem(n.words)
        if p is None and len(text) >= MIN_TEXT:
            made_up = ungrounded(n.words, focus)
            if made_up:
                where = "in the background, not the window in front" if not ungrounded(n.words, text) else \
                    "not on the screen"
                p = (f"these words are {where}: {', '.join(made_up)}. Use only words from the window in front, "
                     "or plain words for what it is")
        return p

    order = ["vision", "text"] if ctx.config.get("prefer", "vision") == "vision" else ["text", "vision"]
    why = "no usable name"
    pic = shrink(data, int(ctx.config.get("vision_max_side", 1280)))
    for route in order:  # the local models first; Claude only when both routes fail
        try:
            if route == "vision":
                task: dict = {"task": "name this screenshot"}
                if screen.get("focus"):
                    task["text_near_the_middle"] = screen["focus"][:15]  # a hint; may still include background
                n = ctx.llm(PLAYBOOK, task, schema=Name, check=check, tiers=["V1"], images=[pic], claude_last=False)
                return clean(n.words), "vision (V1)"
            if len(text) >= MIN_TEXT:
                n = ctx.llm(PLAYBOOK, {"lines_most_prominent_first": screen["focus"]}, schema=Name, check=check,
                            tiers=ctx.local_tiers(), claude_last=False)
                return clean(n.words), f"text ({ctx.last_answer.tier})"
        except EscalationExhausted as e:
            why = str(e)
            if route == "vision" and ("not available" in why or "V1" in why):
                why += " - is V1 set in argus.yaml and qwen2.5vl:7b pulled?"
    try:  # Claude sees the picture; the text on screen is a hint, not a rule here
        task = {"task": "name this screenshot"}
        if screen.get("focus"):
            task["text_near_the_middle"] = screen["focus"][:15]
        n = ctx.claude(PLAYBOOK, task, schema=Name, check=lambda n, _i: problem(n.words), images=[pic],
                       advice=f"The local models could not name it: {why[:300]}")
        return clean(n.words), f"Claude ({ctx.last_answer.tier})"
    except EscalationExhausted as e:
        why = f"{why[:120]}; Claude: {str(e)[:120]}"
    return None, f"no usable name ({why[:240]})"


def rename_one(ctx: Context, path: str) -> dict:
    name = os.path.basename(path)
    if not os.path.exists(path):
        return {"file": name, "skipped": "already gone"}
    if not is_default_name(name):
        return {"file": name, "skipped": "already has a real name"}
    st = ctx.files.stat(path)
    data = ctx.files.read_bytes(path)
    words, how = suggest(ctx, path, data)
    if not words:
        return {"file": name, "skipped": how, "ask": True}
    new = f"{date_for(name, st.st_mtime)} {words}{os.path.splitext(name)[1].lower()}"
    to = ctx.files.move(path, os.path.join(os.path.dirname(path), new))
    return {"file": name, "renamed": os.path.basename(to), "how": how}


def ask_you(ctx: Context, path: str, skipped: dict) -> dict:
    """Nobody could name it: you do (Helios and the phone show the picture). Rejecting leaves it as it is."""
    name = os.path.basename(path)
    if ctx.dry_run or not os.path.exists(path) or not is_default_name(name):
        return skipped
    thumb = shrink(ctx.files.read_bytes(path), 640)
    if len(thumb) > 250_000:
        thumb = b""
    got = ctx.ask_me("Name this screenshot", {"name": ""}, image=thumb or None,
                     summary=[name, "Argus and Claude couldn't name it. Type 3 to 6 words, or Reject to leave it."])
    words = clean((got or {}).get("name") or "")
    if not words:
        return {**skipped, "ask": False, "skipped": "you left it as it is"}
    new = f"{date_for(name, ctx.files.stat(path).st_mtime)} {words}{os.path.splitext(name)[1].lower()}"
    to = ctx.files.move(path, os.path.join(os.path.dirname(path), new))
    return {"file": name, "renamed": os.path.basename(to), "how": "you"}


# ------------------------------------------------------------------ workflows

@workflow(PLUGIN, "name")
def name(ctx: Context):
    """One new screenshot (from the folder watch)."""
    path = str(ctx.input.get("path") or "")
    if not path:
        raise PermanentError("no path in the job input")
    out = ctx.step("rename", rename_one, ctx, path)
    if out.get("ask"):
        out = ctx.step("ask you", ask_you, ctx, path, out)
    ctx.emit("named", **{k: v for k, v in out.items() if k in ("renamed", "skipped")}, dry_run=ctx.dry_run)
    return {**out, "dry_run": ctx.dry_run}


@workflow(PLUGIN, "sweep")
def sweep(ctx: Context):
    """Every screenshot with a default name in the watched folders (the "Name screenshots now" button)."""

    def find():
        found = []
        home = Path(os.path.expanduser("~"))
        for d in ("Pictures/Screenshots", "OneDrive/Pictures/Screenshots", "Desktop", "OneDrive/Desktop"):
            folder = home / d
            if not folder.is_dir():
                continue
            for p in ctx.files.list(folder):
                n = os.path.basename(p)
                if is_default_name(n) and (not d.endswith("Desktop") or n.lower().startswith("screenshot")) \
                        and time.time() - ctx.files.stat(p).st_mtime > 20:
                    found.append(p)
        return sorted(found)[: int(ctx.config.get("max_per_sweep", 30))]

    paths = ctx.step("find", find)
    results = [ctx.step(f"rename {i}", rename_one, ctx, p) for i, p in enumerate(paths)]
    for i, (p, r) in enumerate(zip(paths, results, strict=True)):  # the ones nobody could name: one at a time
        if r.get("ask"):
            results[i] = ctx.step(f"ask you {i}", ask_you, ctx, p, r)
    ctx.emit("named", renamed=sum(1 for r in results if "renamed" in r), dry_run=ctx.dry_run)
    return {"results": results, "dry_run": ctx.dry_run}


@workflow(PLUGIN, "undo")
def undo(ctx: Context):
    """Put the old name back: input {"from": the new path, "to": the old path}."""
    src, dst = str(ctx.input.get("from") or ""), str(ctx.input.get("to") or "")
    if not src or not dst:
        raise PermanentError('undo needs {"from": ..., "to": ...}')
    if not os.path.exists(src):
        raise PermanentError(f"{src} is no longer there")
    return {"back_to": ctx.step("undo", ctx.files.move, src, dst), "dry_run": ctx.dry_run}
