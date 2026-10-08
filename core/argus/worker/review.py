"""The nightly review (job guidance.review): Claude turns the local models' mistakes into short lessons; the eval
set shows whether they help; you decide.

For each playbook in the job's input:
1. Claude reads the playbook, the lessons in force, and the mistakes (wrong answers with what they should have
   been; answers the first tier got wrong) and writes 1-6 short lessons.
2. The eval set (answers you marked correct, else ones the first tier got right) is replayed on the first local
   tier with the old lessons and with the new ones; an answer passes when it matches the kept one.
3. You get the lessons with both scores (Helios and the phone). Approved: they are appended to that playbook for
   that plugin from the next job on. Lessons that pass fewer tests than the playbook does now are not offered at
   all ("didn't help").

Several candidates (the GEPA idea, "reflective prompt evolution"): after the first set of lessons is scored, Claude
sees the tests it still fails (input, the right answer, what the model said) and writes an improved set; the best
scoring set is the one offered (`guidance.candidates` sets at most, default 3; a tie goes to the shorter one).

Also here: guidance.evals ("Run tests now" on a playbook's Learning tab) and guidance.replay ("Try with another
model" on one kept answer).
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

IMPROVE = """You improve lessons for a small local model (rules appended to its instructions). You get the
instructions ("playbook"), the current lessons and the tests they still fail: the input, the right answer and what
the model answered with these lessons. Rewrite the lessons so the model gets these right too, without breaking what
already works: 1 to 6 short, general rules, one per line starting with "- " (no answers copied from one case).
Answer with only the lines."""


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
    passed, failed, misses = 0, [], []
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
            if len(misses) < 5:  # what the next candidate learns from
                misses.append({"input": s["input"], "right_answer": expected,
                               "model_answered": got.model_dump() if hasattr(got, "model_dump") else got})
    return {"passed": passed, "total": len(evals), "failed": failed[:10], "misses": misses,
            "tier": tiers[0] if tiers else None}


def lines_of(text: Any) -> str:
    lines = [ln.strip() for ln in str(text).splitlines() if ln.strip().startswith(("-", "*"))]
    return "\n".join("- " + ln.lstrip("-* ").strip() for ln in lines[:6])


def better(a: dict | None, b: dict | None, la: str, lb: str) -> bool:
    """Is candidate b better than a? More tests passed; on a tie, fewer lines (shorter instructions)."""
    if b is None:
        return False
    if a is None:
        return True
    return b["passed"] > a["passed"] or (b["passed"] == a["passed"] and len(lb.splitlines()) < len(la.splitlines()))


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
            return lines_of(text)

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
        # more candidates: Claude sees what the best set still gets wrong and improves it; the best one is kept
        tries = max(1, int(ctx.input.get("candidates") or 1))
        for n in range(2, tries + 1):
            if not after or not after.get("misses"):
                break  # nothing left to fix (or no tests)

            def improve(pb=pb, lessons=lessons, after=after) -> str:
                return lines_of(ctx.claude(IMPROVE, json.dumps(
                    {"playbook": pb["playbook"], "lessons": lessons, "still_wrong": after["misses"]},
                    ensure_ascii=False, default=str)))

            try:
                cand = ctx.step(f"lessons {i + 1}.{n}", improve)
            except EscalationExhausted:
                break
            if not cand:
                break
            got = ctx.step(f"evals after {i + 1}.{n}", score, ctx, pb, cand)
            if better(after, got, lessons, cand):
                lessons, after = cand, got
        evals = {"before": before, "after": after}
        if before:
            ctx.step(f"record {i + 1}", lambda pb=pb, before=before: ctx.tool(
                "record_evals", {"key": pb["key"], "run": before, "why": "review", "job_id": ctx.job_id}))
        if before and after and after["passed"] < before["passed"]:
            out.append({"playbook": pb["name"], "plugin": pb["plugin"], "lessons": lessons, "evals": evals,
                        "skipped": f"didn't help: {after['passed']}/{after['total']} tests with them, "
                                   f"{before['passed']}/{before['total']} without"})
            continue
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


@workflow("guidance", "evals")
def evals(ctx: Context):
    """Run a playbook's tests now: the eval set on the first local tier, with the lessons in force."""
    pb = ctx.input.get("playbook") or {}
    if not pb.get("evals"):
        raise PermanentError("no tests yet: mark some answers Correct in Helios")
    run = ctx.step("tests", score, ctx, pb, pb.get("lessons") or "")
    ctx.step("record", lambda: ctx.tool("record_evals", {"key": pb["key"], "run": run, "why": "manual",
                                                         "job_id": ctx.job_id}))
    if ctx.input.get("compare") and pb.get("lessons"):  # what the lessons change: the same tests without them
        bare = ctx.step("tests without lessons", score, ctx, pb, "")
        ctx.step("record without", lambda: ctx.tool("record_evals", {"key": pb["key"], "run": bare,
                                                                     "why": "without lessons", "job_id": ctx.job_id}))
        return {**run, "without_lessons": bare}
    return run


@workflow("guidance", "replay")
def replay(ctx: Context):
    """The same question to another tier (same playbook and lessons): both answers, and whether they agree."""
    s = ctx.input.get("sample") or {}
    tier = str(ctx.input.get("tier") or "")
    if not s or not tier:
        raise PermanentError("needs an answer and a tier")
    inp = s["input"] if isinstance(s["input"], str) else json.dumps(s["input"], ensure_ascii=False)

    def ask() -> dict[str, Any]:
        try:
            got = ctx.llm(with_lessons(ctx.input.get("playbook") or "", ctx.input.get("lessons") or ""), inp,
                          schema=ctx.input.get("schema") or None, tiers=[tier], attempts=1, claude_last=False)
            return {"answer": got, "error": None}
        except EscalationExhausted as e:
            return {"answer": None, "error": str(e)[:300]}

    got = ctx.step(f"ask {tier}", ask)
    kept = s["correction"] if s.get("verdict") == "correct" and s.get("correction") else s["output"]
    return {"tier": tier, "answer": got["answer"], "error": got["error"], "kept_tier": s.get("tier"), "kept": kept,
            "same": got["answer"] is not None and same(kept, got["answer"])}
