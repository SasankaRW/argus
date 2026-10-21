"""Ari's memory over time, written on the PC by the local model in the background (never slowing a reply).

ari.summarize: the turns of a chat that fell out of the model's window, folded into a short running summary (the
model then sees the last turns word for word and this for everything earlier that day).
ari.episode: a chat that went quiet, in 2-3 lines (what you asked, what worked, what didn't) and what was left open;
kept and searchable across days, and the matching ones are given to Ari with a new message.

Both keep what they wrote through argusd's built-in tools (keep_summary, keep_episode). argusd never sends a chat
where something private was used (the screen, the clipboard, mail, money), and the local model writes these, never
Claude.
"""

from __future__ import annotations

import json
import re

from pydantic import BaseModel, Field

from ..models import EscalationExhausted
from .workflows import Context, PermanentError, workflow

SUMMARY = """You keep a short running summary of a conversation between the user and Ari, their assistant.
You get the summary so far (may be empty) and the turns that came after it. Write the new summary: 2-5 short
sentences, past tense, plain facts only: what the user asked or told, what Ari did or found, what is still going on
or was left open. Keep names, apps, files, songs and numbers exactly as said. Leave out small talk and anything
already done and no longer relevant. Never add anything that wasn't said. Answer with JSON only."""

EPISODE = """You write a short memory of a finished conversation between the user and Ari, their assistant, so that
next time Ari knows what happened. "episode": 1-3 short sentences, past tense: what the user wanted, what worked and
what didn't (and why, if it was said). "left_open": what was asked but not done or left to do later, in a few words,
or empty. Keep names, apps, files and numbers exactly as said. Never add anything that wasn't said. Small talk only:
episode "just chatted" and left_open empty. Answer with JSON only."""


class Summary(BaseModel):
    summary: str = Field(description="the new running summary, 2-5 short sentences")


class Episode(BaseModel):
    episode: str = Field(description="1-3 short sentences: what was wanted, what worked, what didn't")
    left_open: str = Field("", description="what was left to do, in a few words, or empty")


def _said(turns: list[dict]) -> set[str]:
    return {w.lower() for t in turns for w in re.findall(r"[A-Za-z][\w'-]{2,}", str(t.get("said") or ""))}


def made_up_names(text: str, turns: list[dict], before: str = "") -> list[str]:
    """Capitalised names in `text` that nobody said (the model inventing a person, an app or a place)."""
    said = _said(turns) | {w.lower() for w in re.findall(r"[A-Za-z][\w'-]{2,}", before)}
    common = {"the", "user", "ari", "they", "she", "he", "it", "then", "later", "today", "yesterday", "nothing",
              "just", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}
    out = []
    for m in re.finditer(r"[A-Za-z][\w'-]{2,}", text):
        w = m.group(0)
        if w[0].isupper() and w.lower() not in said and w.lower() not in common:
            prev = text[:m.start()].rstrip()
            if prev and prev[-1] not in ".!?":  # a capital after a full stop is just a new sentence
                out.append(w)
    return out


@workflow("ari", "summarize")
def summarize(ctx: Context):
    turns = [t for t in ctx.input.get("turns") or [] if isinstance(t, dict) and t.get("said")]
    conv, upto = str(ctx.input.get("conv") or ""), int(ctx.input.get("upto") or 0)
    if not conv or not turns:
        raise PermanentError("nothing to summarize")
    before = str(ctx.input.get("summary") or "")

    def check(s: Summary, _inp) -> str | None:
        if not s.summary.strip():
            return "write the summary"
        if len(s.summary) > 900:
            return "too long: 2-5 short sentences"
        bad = made_up_names(s.summary, turns, before)
        return f"{', '.join(bad[:3])} were never said: only what was said" if bad else None

    def write() -> dict:
        s = ctx.llm(SUMMARY, json.dumps({"summary_so_far": before, "turns_after_it": turns}, ensure_ascii=False),
                    schema=Summary, check=check, tiers=ctx.local_tiers() or None, claude_last=False)
        return {"summary": s.summary.strip()}

    try:
        got = ctx.step("summarize", write)
    except EscalationExhausted as e:
        return {"kept": False, "problem": str(e)[:200]}
    ctx.step("keep", ctx.tool, "keep_summary", {"conv": conv, "summary": got["summary"], "upto": upto})
    return {"kept": True, "conv": conv, "summary": got["summary"], "upto": upto}


@workflow("ari", "episode")
def episode(ctx: Context):
    turns = [t for t in ctx.input.get("turns") or [] if isinstance(t, dict) and t.get("said")]
    conv = str(ctx.input.get("conv") or "")
    if not conv or not turns:
        raise PermanentError("nothing to remember")

    def check(e: Episode, _inp) -> str | None:
        if not e.episode.strip():
            return "write the episode"
        if len(e.episode) > 500:
            return "too long: 1-3 short sentences"
        bad = made_up_names(f"{e.episode} {e.left_open}", turns)
        return f"{', '.join(bad[:3])} were never said: only what was said" if bad else None

    def write() -> dict:
        e = ctx.llm(EPISODE, json.dumps({"conversation": turns}, ensure_ascii=False), schema=Episode, check=check,
                    tiers=ctx.local_tiers() or None, claude_last=False)
        return e.model_dump()

    try:
        got = ctx.step("write", write)
    except EscalationExhausted as e:
        return {"kept": False, "problem": str(e)[:200]}
    if got["episode"].strip().lower().rstrip(".") == "just chatted" and not got["left_open"].strip():
        got["episode"] = "Just chatted."
    ctx.step("keep", ctx.tool, "keep_episode", {"conv": conv, "episode": got["episode"],
                                                 "left_open": got.get("left_open") or "",
                                                 "started": float(ctx.input.get("started") or 0),
                                                 "ended": float(ctx.input.get("ended") or 0)})
    return {"kept": True, "conv": conv, **got}
