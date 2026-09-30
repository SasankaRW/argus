"""Screen and clipboard: Ari looks at your screen, or sums up what you copied.

look       grabs the screen in memory (never saved), shrinks it, and asks the vision model (V1). Without V1, the text
           on screen (Tesseract) goes to T1 instead.
clipboard  reads the clipboard: text is summed up by the local text models (T1, then T2); a copied picture goes
           to V1 like the screen.

Local models only (claude_last=False): what is on your screen or clipboard may be work or private, so it never
leaves the PC.
"""

from __future__ import annotations

import io
import platform
import subprocess

from pydantic import BaseModel, Field

from argus.models import EscalationExhausted
from argus.worker import Context, PermanentError, workflow

PLUGIN = "screen"
WIN = platform.system() == "Windows"
MAX_TEXT = 20000


class Answer(BaseModel):
    answer: str = Field(description="the answer, 1-3 short sentences, read aloud")


LOOK = """You are Ari, looking at the user's screen for them. Answer their question about it in 1-3 short sentences
that read well aloud: no lists, no markdown. Say what the main window shows (the app and what is in it); read out
short text that matters (an error, a title, a number). If you can't tell, say so. Answer as JSON {"answer": "..."}."""

LOOK_TEXT = """You are Ari. You can't see the user's screen, only the text on it (read by OCR, most prominent first,
may have mistakes). Answer their question about the screen in 1-3 short sentences that read well aloud. Say plainly
that you read the text on it. Answer as JSON {"answer": "..."}."""

SUM = """You are Ari. Sum up the text the user copied, as they asked (default: the gist in 2-3 short sentences).
It is read aloud: no lists, no markdown; action items or numbers in a plain sentence. Answer as JSON
{"answer": "..."}."""


def check(a: Answer, _inp) -> str | None:
    t = a.answer.strip()
    if not t:
        return "give the answer"
    if len(t) > 700:
        return "too long: 1-3 short sentences"
    if t.lstrip().startswith(("-", "*", "#", "1.")):
        return "no lists or markdown: plain sentences"
    return None


def png(img, max_side: int) -> bytes:
    if max(img.size) > max_side:
        img = img.copy()
        img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def grab():
    """The whole screen as a PIL image (all monitors). Tests replace this."""
    try:
        from PIL import ImageGrab  # type: ignore[import-not-found]
    except ImportError:
        raise PermanentError("needs Pillow (pip install -e .[plugins])") from None
    return ImageGrab.grab(all_screens=True)


def screen_text(img) -> str:
    """The text on screen, via Tesseract, or "" when it isn't installed."""
    try:
        import pytesseract  # type: ignore[import-not-found]

        return " ".join(pytesseract.image_to_string(img).split())[:4000]
    except Exception:
        return ""


def read_clipboard() -> dict:
    """{"text": ...} or {"image": PIL image} or {} (empty). Tests replace this."""
    if not WIN:
        raise PermanentError("the clipboard works on Windows only")
    try:
        from PIL import ImageGrab  # type: ignore[import-not-found]

        got = ImageGrab.grabclipboard()
        if got is not None and not isinstance(got, list):
            return {"image": got}
    except ImportError:
        pass
    p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-STA", "-Command",
                        "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-Clipboard -Raw"],
                       capture_output=True, text=True, timeout=15, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise PermanentError((p.stderr or p.stdout).strip()[:300])
    return {"text": p.stdout} if p.stdout.strip() else {}


def see(ctx: Context, img, question: str) -> dict:
    """Ask V1 about a picture; without V1, T1 about its text."""
    pic = png(img, int(ctx.config.get("max_side", 1280)))
    try:
        a = ctx.llm(LOOK, {"question": question}, schema=Answer, check=check, tiers=["V1"], images=[pic],
                    claude_last=False)
        return {"answer": a.answer.strip(), "how": "vision (V1)"}
    except EscalationExhausted as e:
        text = screen_text(img)
        if not text:
            raise PermanentError(f"no vision model to look with ({str(e)[:120]}), and no text on it to read "
                                 "(Tesseract)") from None
    try:
        a = ctx.llm(LOOK_TEXT, {"question": question, "text_on_screen": text}, schema=Answer, check=check,
                    tiers=ctx.local_tiers() or None, claude_last=False)
    except EscalationExhausted as e:
        raise PermanentError(f"couldn't make sense of it: {str(e)[:200]}") from None
    return {"answer": a.answer.strip(), "how": f"the text on it ({ctx.last_answer.tier})"}


@workflow(PLUGIN, "look")
def look(ctx: Context):
    question = str(ctx.input.get("question") or "").strip() or "What's on my screen?"
    return ctx.step("look", lambda: see(ctx, grab(), question))


@workflow(PLUGIN, "clipboard")
def clipboard(ctx: Context):
    how = str(ctx.input.get("how") or "").strip() or "the gist in 2-3 sentences"

    def go():
        got = read_clipboard()
        if "image" in got:
            return {**see(ctx, got["image"], f"The user copied this picture. {how}"), "copied": "a picture"}
        text = str(got.get("text") or "").strip()
        if not text:
            return {"answer": "The clipboard is empty.", "copied": "nothing"}
        words = len(text.split())
        if words <= 25:  # short: just say it
            return {"answer": f"You copied: {text}", "copied": f"{words} words"}
        try:
            a = ctx.llm(SUM, {"how": how, "copied_text": text[:MAX_TEXT], "cut": len(text) > MAX_TEXT},
                        schema=Answer, check=check, tiers=ctx.local_tiers() or None, claude_last=False)
        except EscalationExhausted as e:
            raise PermanentError(f"couldn't sum it up: {str(e)[:200]}") from None
        return {"answer": a.answer.strip(), "copied": f"{words} words"}

    return ctx.step("sum up", go)
