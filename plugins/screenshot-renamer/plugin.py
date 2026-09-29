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
FILLER = {"a", "an", "the", "of", "on", "in", "with", "and", "for", "to", "some", "my"}
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

def ocr(data: bytes) -> str:
    """Text in the picture, or "" when Tesseract (or Pillow/pytesseract) isn't installed."""
    try:
        import pytesseract  # type: ignore[import-not-found]
        from PIL import Image  # type: ignore[import-not-found]
    except ImportError:
        return ""
    if not shutil.which("tesseract") and not os.path.exists(r"C:\Program Files\Tesseract-OCR\tesseract.exe"):
        return ""
    if not shutil.which("tesseract"):
        pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    try:
        text = pytesseract.image_to_string(Image.open(io.BytesIO(data)), timeout=30)
    except Exception:  # a broken image or a Tesseract error: fall back to the vision model
        return ""
    return re.sub(r"\s+", " ", text).strip()


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
    words: str = Field(description="3 to 6 lowercase words saying what the screenshot shows")


PLAYBOOK = """You name screenshots so they are easy to find later.
Reply with 3 to 6 lowercase words saying what the screenshot shows: the app or site, and the thing on screen
(an error, a page, a chart, a conversation). Prefer specific words: "cashly login bug", "github pr review argus",
"vscode python traceback", "bank transfer receipt". No dates, no times, no file extension, no punctuation.
Answer as JSON: {"words": "<3 to 6 words>"}"""


def suggest(ctx: Context, path: str, data: bytes) -> tuple[str | None, str]:
    """-> (words, how). The text route first when the picture has enough text, else the vision model."""

    def check(n: Name, _inp) -> str | None:
        return problem(n.words)

    text = ocr(data)
    if len(text) >= MIN_TEXT:
        try:
            n = ctx.llm(PLAYBOOK, {"text_on_screen": text[:1500]}, schema=Name, check=check)
            return clean(n.words), f"text ({ctx.last_answer.tier})"
        except EscalationExhausted:
            pass  # the text was not enough: let the vision model look
    try:
        pic = shrink(data, int(ctx.config.get("vision_max_side", 1280)))
        task = {"task": "name this screenshot", "text_found": text[:300]} if text else "name this screenshot"
        n = ctx.llm(PLAYBOOK, task, schema=Name, check=check, tiers=["V1"], images=[pic])
        return clean(n.words), "vision (V1)"
    except EscalationExhausted as e:
        why = str(e)
        if "not available" in why or "V1" in why:
            why += " - is V1 set in argus.yaml and qwen2.5vl:7b pulled?"
        return None, f"no usable name ({why[:160]})"


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
        return {"file": name, "skipped": how}
    new = f"{date_for(name, st.st_mtime)} {words}{os.path.splitext(name)[1].lower()}"
    to = ctx.files.move(path, os.path.join(os.path.dirname(path), new))
    return {"file": name, "renamed": os.path.basename(to), "how": how}


# ------------------------------------------------------------------ workflows

@workflow(PLUGIN, "name")
def name(ctx: Context):
    """One new screenshot (from the folder watch)."""
    path = str(ctx.input.get("path") or "")
    if not path:
        raise PermanentError("no path in the job input")
    out = ctx.step("rename", rename_one, ctx, path)
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
