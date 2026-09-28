"""Built-in demo plugin, so a fresh install has something to run.

    POST /jobs {"plugin": "demo", "workflow": "echo",  "input": {"text": "hi"}}
    POST /jobs {"plugin": "demo", "workflow": "sleep", "input": {"seconds": 2, "steps": 3}}
    POST /jobs {"plugin": "demo", "workflow": "fail",  "input": {"permanent": false}}
    POST /jobs {"plugin": "demo", "workflow": "classify", "input": {"filename": "lecture_07.pdf"},
                "needs": []}
        asks the models to sort a file into a folder; add "reject_tier": "T1" to see an escalation
    POST /jobs {"plugin": "demo", "workflow": "approval", "input": {"vendor": "CEB", "amount": "4,250.00"}}
        a pretend bill: waits for your approval (phone or Helios), then "files" it and sends a message
    POST /jobs {"plugin": "demo", "workflow": "batch", "input": {"count": 4}}
        one approval for a batch of pretend payments; Argus adds up the total
"""

from __future__ import annotations

import time

from pydantic import BaseModel, Field

from ..approvals import fmt_money, money
from .workflows import Context, PermanentError, workflow

CATEGORIES = ["Documents", "Images", "Videos", "Music", "Archives", "Installers", "Code", "Misc"]


class Placement(BaseModel):
    category: str = Field(description="one of the allowed categories")
    reason: str = Field(description="a few words on why")


@workflow("demo", "echo")
def echo(ctx: Context):
    text = ctx.step("read", lambda: str(ctx.input.get("text", "")))
    words = ctx.step("count", lambda: len(text.split()))
    return {"text": text, "words": words}


@workflow("demo", "sleep")
def sleep(ctx: Context):
    seconds = float(ctx.input.get("seconds", 1))
    for i in range(int(ctx.input.get("steps", 3))):
        ctx.step(f"nap-{i}", time.sleep, seconds)
    return {"slept": seconds * int(ctx.input.get("steps", 3))}


@workflow("demo", "fail")
def fail(ctx: Context):
    def boom():
        if ctx.input.get("permanent"):
            raise PermanentError("asked to fail for good")
        raise RuntimeError("asked to fail")

    ctx.step("boom", boom)


@workflow("demo", "classify")
def classify(ctx: Context):
    """The downloads-organizer pattern in miniature: a model decides, code checks, escalation fixes."""
    name = str(ctx.input.get("filename", "lecture_07.pdf"))
    reject = ctx.input.get("reject_tier")  # force a tier's answer to be rejected, to watch escalation

    def check(p: Placement, _inp) -> str | None:
        if reject and ctx._router.current_tier == reject:
            return f"demo: answers from {reject} are rejected on purpose"
        if p.category not in CATEGORIES:
            return f"{p.category!r} is not an allowed category; use one of {', '.join(CATEGORIES)}"
        return None

    def decide():
        p = ctx.llm(
            "You sort downloaded files into folders. Pick the single best folder for the file from the "
            f"allowed categories: {', '.join(CATEGORIES)}.",
            {"filename": name, "allowed_categories": CATEGORIES},
            schema=Placement, check=check,
        )
        return {"category": p.category, "reason": p.reason, "tier": ctx.last_answer.tier,
                "attempts": ctx.last_answer.attempts}

    return ctx.step("classify", decide)


@workflow("demo", "approval")
def approval(ctx: Context):
    """The bill-filer pattern: parse, ask you (money always needs you), then act on what you approved."""

    def parse():
        amount = money(ctx.input.get("amount", "4250"))
        if amount is None:
            raise PermanentError(f"not an amount: {ctx.input.get('amount')!r}")
        return {"vendor": str(ctx.input.get("vendor", "CEB")), "amount": fmt_money(amount),
                "due": str(ctx.input.get("due", "2026-10-15"))}

    bill = ctx.step("parse", parse)

    def ask():
        d = ctx.approve("entry", f"{bill['vendor']} bill", bill)
        return {"approved": d.approved, "state": d.state, "fields": d.fields, "by": d.by}

    decision = ctx.step("approve", ask)
    if not decision["approved"]:
        return {"filed": False, "because": decision["state"]}

    def file_it():
        f = decision["fields"]
        ctx.notify(f"Filed: {f['vendor']} bill", f"{f['amount']} due {f['due']}", tags=["white_check_mark"])
        return {"filed": True, **f}

    return ctx.step("file", file_it)


@workflow("demo", "batch")
def batch(ctx: Context):
    n = int(ctx.input.get("count", 4))
    items = [{"label": f"Payment {i + 1}", "amount": f"{(i + 1) * 1250.5:.2f}"} for i in range(n)]

    def ask():
        d = ctx.approve("batch", f"{n} payments", items=items)
        return {"approved": d.approved, "state": d.state}

    return ctx.step("approve", ask)
