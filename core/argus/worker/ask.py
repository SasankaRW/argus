"""Ask Argus, the model part: argusd hands over the question, a snapshot of Argus and the list of actions; a small
model answers in a sentence or two and may suggest one action from the list (you still tap to run it)."""

from __future__ import annotations

import json

from pydantic import BaseModel, Field

from .workflows import Context, PermanentError, workflow


class Answer(BaseModel):
    reply: str = Field(description="1-2 short sentences, plain words")
    action: str = Field("", description="one action id from the list, or empty")


PLAYBOOK = """You are Argus, a home automation assistant. Answer the user's message in 1-2 short, plain sentences.

You get: the message, a snapshot of Argus right now (running and queued jobs, what waits for the user, recent
results, power), and a list of actions (id, label, about).
- A question: answer only from the snapshot. If it isn't there, say you don't know. Never invent jobs or results.
- A request to do something: pick the ONE action whose label fits, put its id in "action", and say what it will do.
  Only ids from the list. If nothing fits, say so and leave "action" empty.
Answer as JSON: {"reply": "...", "action": "<id or empty>"}"""


@workflow("ask", "ask")
def ask(ctx: Context):
    text = str(ctx.input.get("text") or "").strip()
    if not text:
        raise PermanentError("nothing asked")
    actions = ctx.input.get("actions") or []
    ids = {a["id"] for a in actions}

    def check(a: Answer, _inp) -> str | None:
        if not a.reply.strip():
            return "reply is empty"
        if a.action and a.action not in ids:
            return f"{a.action!r} is not one of the action ids; pick from the list or leave it empty"
        if len(a.reply) > 400:
            return "too long: 1-2 short sentences"
        return None

    def answer():
        task = {"message": text, "snapshot": ctx.input.get("snapshot") or {},
                "actions": [{k: a[k] for k in ("id", "label", "about")} for a in actions]}
        a = ctx.llm(PLAYBOOK, json.dumps(task, ensure_ascii=False), schema=Answer, check=check, tiers=["T1", "T2"])
        label = next((x["label"] for x in actions if x["id"] == a.action), None)
        return {"reply": a.reply.strip(), "action": a.action or None, "label": label, "tier": ctx.last_answer.tier}

    return ctx.step("answer", answer)
