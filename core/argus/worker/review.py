"""The nightly review (job guidance.review): Claude turns the local models' mistakes into short lessons; the eval
set shows whether they help; you decide.

For each playbook in the job's input:
1. Claude reads the playbook, the lessons in force, and the mistakes (wrong answers with what they should have
   been; answers the first tier got wrong) and writes 1-6 short lessons.
2. The eval set (answers you marked correct, else ones the first tier got right) is replayed on the first local
   tier with the old lessons and with the new ones; an answer passes when it matches the kept one.
3. You get the lessons with both scores (Helios and the phone). Approved: they are appended to that playbook for
   that plugin from the next job on.
"""

from __future__ import annotations

import json
from typing import Any

from ..models import EscalationExhausted
from .workflows import Context, PermanentError, workflow

REVIEW = """You improve the instructions ("playbook") that a small local language model follows for one task in the
user's home automation. You get the playbook, the lessons already added to it, and recent mistakes: the input,
what the model answered, and what was right (the user's correction, or the answer a bigger model gave after the
small one's was rejected).

Write the lessons the small model needs so it stops making these mistakes: 1 to 6 short, concrete rules, one per
line starting with "- ". General rules, not one-off answers (no file names or values copied from a single case
unless they are a pattern). Keep lessons already in force that still apply; drop ones the mistakes contradict.
Answer with only the lines."""

MAX_EVALS = 20


def same(expected: Any, got: Any) -> bool:
    """Does the replayed answer match the kept one? For JSON objects only the kept keys count; text is compared
    loosely (case, spaces)."""
    if isinstance(expected, dict) and isinstance(got, dict):
        return all(same(v, got.get(k)) for k, v in expected.items())
    if isinstance(expected, list) and isinstance(got, list):
        return len(expected) == len(got) and all(same(a, b) for a, b in zip(expected, got, strict=False))
    if isinstance(expected, str) and isinstance(got, str):
        return " ".join(expected.lower().split()) == " ".join(got.lower().split())
    return expected == got


def with_lessons(playbook: str, lessons: str) -> str:
    return f"{playbook}\n\nLessons from earlier mistakes (follow them):\n{lessons}" if lessons.strip() else playbook


def score(ctx: Context, pb: dict[str, Any], lessons: str) -> dict[str, Any]:
    """Replay the eval set on the first local tier. {passed, total, failed: [sample ids]}."""
    tiers = ctx.local_tiers()[:1]
    evals = pb["evals"][:MAX_EVALS]
    passed, failed = 0, []
    for s in evals:
        expected = s["correction"] if s.get("verdict") == "correct" and s.get("correction") else s["output"]
        inp = s["input"] if isinstance(s["input"], str) else json.dumps(s["input"], ensure_ascii=False)
        try:
            got = ctx.llm(with_lessons(pb["playbook"], lessons), inp, schema=pb.get("schema") or None, tiers=tiers,
                          attempts=1, claude_last=False)
        except EscalationExhausted:
            got = None
        if same(expected, got):
            passed += 1
        else:
            failed.append(s["id"])
    return {"passed": passed, "total": len(evals), "failed": failed[:10]}


@workflow("guidance", "review")
def review(ctx: Context):
    books = ctx.input.get("playbooks") or []
    if not books:
        raise PermanentError("nothing to review")
    out = []
    for i, pb in enumerate(books):
        def write(pb=pb) -> str:
            mistakes = [{"input": m["input"], "model_answered": m["output"],
                         "right_answer": m["correction"] if m.get("correction") else
                         ("(a bigger model answered this; the small model's first answer was rejected)"
                          if m.get("escalated") else "(the user marked it wrong)")}
                        for m in pb["mistakes"]]
            text = ctx.claude(REVIEW, json.dumps({"playbook": pb["playbook"], "lessons_in_force": pb["lessons"],
                                                  "mistakes": mistakes}, ensure_ascii=False, default=str))
            lines = [ln.strip() for ln in str(text).splitlines() if ln.strip().startswith(("-", "*"))]
            return "\n".join("- " + ln.lstrip("-* ").strip() for ln in lines[:6])

        try:
            lessons = ctx.step(f"lessons {i + 1}", write)
        except EscalationExhausted as e:
            out.append({"playbook": pb["name"], "skipped": f"Claude couldn't help: {str(e)[:120]}"})
            continue
        if not lessons:
            out.append({"playbook": pb["name"], "skipped": "no lessons"})
            continue
        before = ctx.step(f"evals before {i + 1}", score, ctx, pb, pb["lessons"]) if pb["evals"] else None
        after = ctx.step(f"evals after {i + 1}", score, ctx, pb, lessons) if pb["evals"] else None
        evals = {"before": before, "after": after}
        lid = ctx.step(f"keep {i + 1}", lambda pb=pb, lessons=lessons, evals=evals: ctx.tool(
            "propose_lessons", {"key": pb["key"], "text": lessons, "evals": evals, "job_id": ctx.job_id}))["lesson"]

        def ask(pb=pb, lessons=lessons, before=before, after=after) -> bool:
            sc = (f"Tests: {before['passed']}/{before['total']} now, {after['passed']}/{after['total']} with these."
                  if before and after else "No tests yet: mark some answers Correct in Helios to get scores.")
            d = ctx.approve("draft", f"New lessons for {pb['plugin']}",
                            summary=[f"Playbook: {pb['name']}", f"From {len(pb['mistakes'])} recent mistakes.", sc,
                                     *lessons.splitlines()])
            return bool(d)

        yes = ctx.step(f"ask {i + 1}", ask)
        ctx.step(f"decide {i + 1}",
                 lambda lid=lid, yes=yes: ctx.tool("decide_lessons", {"lesson": lid, "approve": yes}))
        out.append({"playbook": pb["name"], "plugin": pb["plugin"], "lessons": lessons, "evals": evals,
                    "approved": yes})
    return {"reviewed": out}
