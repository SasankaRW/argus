"""Ari's thinking: a question or a request in, the tools it needs, an answer out (job ari.think, queued by argusd
for anything the instant rules can't answer).

    you: "open spotify and turn the volume down"      -> open_app(spotify), set_volume(down) -> "Done."
    you: "what did I write about the laptop server?"  -> search_files("laptop server") -> answer from your files
    you: "who won the match yesterday?"               -> needs the web -> Claude with web search

Local first: the model (T1, then T2) chooses one step at a time: a tool, or the answer. Questions about your own
things always go through a tool (your files, Argus's data); general knowledge it answers itself when sure. When it
needs something current or isn't sure, Claude answers with web search (read only), within the daily cap.

A tool marked "asks first" (closing apps, typing, changing files) is not run: Ari asks, and your yes runs it.
"""

from __future__ import annotations

import json
import time
from typing import Any

from pydantic import BaseModel, Field

from ..models import EscalationExhausted
from .workflows import Context, PermanentError, ToolFailed, workflow

MAX_STEPS = 6


class Step(BaseModel):
    tool: str = Field("", description="a tool name from the list, or empty when answering")
    args: dict[str, Any] = Field(default_factory=dict)
    reply: str = Field("", description="the answer to the user, when no tool is needed any more")
    need_web: bool = Field(False, description="true when the answer needs current information from the internet")


PLAYBOOK = """You are Ari, the user's personal assistant on their own computer (home automation system "Argus").
You talk like a warm, capable person. Your replies are read aloud: 1-3 short sentences, no lists, no markdown.

Each turn you get: the user's message, the conversation so far, the tools you can use, and the results of tools
you already used for this message. Decide ONE next step and answer as JSON:
  {"tool": "<name>", "args": {...}}                    to use a tool, or
  {"reply": "<what you say>"}                           when you can answer or have done what was asked, or
  {"need_web": true}                                    when the answer needs current information from the internet.

How to decide:
- "you_remember" holds facts the user asked you to remember that may matter here: use them first. If the user
  asks you to remember something, use the remember tool; to forget something, recall_memory then forget_memory.
- The user's own things (their files, notes, documents, projects, what Argus did, their schedules, their phone,
  their PC): use a tool first; never make up the user's data. If the tools find nothing, say so.
- Doing things on the PC (open an app, a file or a site, volume, music, windows): use the tool. Several things
  asked: one tool per step, then reply once everything is done.
- General knowledge (how something works, definitions, maths, advice): answer yourself if you are sure.
- Current things (news, weather, prices, scores, today's events, anything after your training) or when you are
  not sure: {"need_web": true}.
- A tool marked "asks_first": still choose it; the user will be asked before it runs.
- Reply naturally about what happened ("Opened Spotify and turned it down."). If a tool failed, say what went wrong
  in plain words.
Answer with the JSON only."""

WEB = """You are Ari, the user's personal assistant. Answer the user's question using web search when it needs
current information. Your answer is read aloud: 2-4 short sentences, no lists, no markdown, no links (mention the
source by name if it matters). If something the user's own files said is included, prefer it for their own
matters. Answer with only the reply text."""


def _clip(v: Any, n: int = 1500) -> Any:
    s = json.dumps(v, ensure_ascii=False, default=str)
    return v if len(s) <= n else s[:n] + "…"


@workflow("ari", "think")
def think(ctx: Context):
    text = str(ctx.input.get("text") or "").strip()
    if not text:
        raise PermanentError("nothing said")
    tools = {t["name"]: t for t in ctx.input.get("tools") or []}
    base = {"message": text, "conversation_so_far": (ctx.input.get("history") or [])[-8:],
            "you_remember": ctx.input.get("you_remember") or [],
            "now": ctx.input.get("now") or time.strftime("%A %d %B %Y, %H:%M"),
            "tools": list(tools.values())}
    done: list[dict[str, Any]] = []

    def check(s: Step, _inp) -> str | None:
        if s.tool:
            if s.tool not in tools:
                return f"{s.tool!r} is not one of the tools; use a name from the list, or reply"
            missing = [r for r in tools[s.tool].get("required", []) if not s.args.get(r)]
            if missing:
                return f"{s.tool} needs {', '.join(missing)}"
            if any(d["tool"] == s.tool and d["args"] == s.args for d in done):
                return "you already used that tool with these arguments; use its result and reply"
        elif not s.need_web and not s.reply.strip():
            return "give a reply, a tool, or need_web"
        if len(s.reply) > 600:
            return "too long: 1-3 short sentences"
        return None

    for i in range(MAX_STEPS):
        def decide(i=i) -> dict:
            task = {**base, "results_so_far": done} if done else base
            s = ctx.llm(PLAYBOOK, json.dumps(task, ensure_ascii=False), schema=Step, check=check,
                        tiers=ctx.local_tiers() or None, claude_last=True)
            return {**s.model_dump(), "tier": ctx.last_answer.tier}

        try:
            s = ctx.step(f"think {i + 1}", decide)
        except EscalationExhausted as e:
            return {"reply": "Sorry, I couldn't work that out just now.", "error": str(e)[:200], "used": done}
        if s["tool"]:
            t = tools[s["tool"]]
            if t.get("asks_first"):
                ask = s["reply"].strip() or f"Shall I {t['does'][0].lower()}{t['does'][1:].rstrip('.')}?"
                return {"reply": ask, "pending": {"kind": "tool", "name": s["tool"], "args": s["args"]}, "used": done}

            def use(s=s) -> dict:
                try:
                    return {"tool": s["tool"], "args": s["args"], "result": _clip(ctx.tool(s["tool"], s["args"]))}
                except ToolFailed as e:
                    return {"tool": s["tool"], "args": s["args"], "error": str(e)[:300]}

            done.append(ctx.step(f"tool {i + 1}: {s['tool']}", use))
            continue
        if s["need_web"]:
            def web() -> dict:
                ctx.claude(WEB, json.dumps({"question": text, "conversation_so_far": base["conversation_so_far"],
                                            "found_locally": done}, ensure_ascii=False), web=True)
                return {"reply": str(ctx.last_answer.value).strip()[:1200], "tier": ctx.last_answer.tier}

            try:
                w = ctx.step("web", web)
                return {"reply": w["reply"], "used": done, "via": "web", "tier": w["tier"]}
            except EscalationExhausted:
                if s["reply"]:
                    return {"reply": s["reply"], "used": done}
                return {"reply": "That needs the internet, and I can't reach Claude right now.", "used": done}
        return {"reply": s["reply"].strip(), "used": done, "tier": s.get("tier")}
    return {"reply": "That took more steps than I can do at once. Could you ask for one thing at a time?",
            "used": done}
