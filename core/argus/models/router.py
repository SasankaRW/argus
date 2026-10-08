"""The tier router: ask the cheapest model that can do the job, check the answer in code, escalate if not.

    answer = router.ask(
        playbook="Classify the file into one of the allowed categories.",
        input={"filename": "lecture_07.pdf"},
        schema=Category,                         # a Pydantic model: the answer must parse into it
        check=lambda out, inp: None if out.category in ALLOWED else f"{out.category!r} is not allowed",
    )
    answer.value     # a Category instance
    answer.tier      # "T1", or "T2" if T1 kept failing

For each tier in the chain (default T1 -> T2 -> T3):
  1. Ask the board for a permit (circuit breaker closed? Claude budget left?). No permit: skip the tier.
  2. Call the model. A timeout or error is reported to the board (breaker) and moves on to the next tier.
  3. Parse the reply as JSON, validate it against the schema, run the check. All good: done.
     Otherwise try again on the same tier, telling the model what was wrong (`attempts_per_tier` times).
  4. Still failing: escalate. The next tier sees the input plus what the lower tier answered and why it
     was rejected, so it gets advice for free.

Every request, reply, failed check and escalation is sent as an event, so Helios draws plugin -> T1,
T1 -> T2 and so on, and the job's history shows the whole conversation.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from .providers import ModelError, ModelTimeout, Reply


class EscalationExhausted(Exception):
    """Every tier in the chain failed or was unavailable. Retrying the job later may succeed."""

    def __init__(self, message: str, trail: list[dict[str, Any]]):
        super().__init__(message)
        self.trail = trail


class Provider(Protocol):
    kind: str

    def chat(self, system: str, messages: list[dict[str, str]], schema: dict | None = None) -> Reply: ...


class Board(Protocol):
    """What the router needs from argusd (or a fake in tests)."""

    def permit(self, tier: str) -> dict[str, Any]: ...

    def report(self, tier: str, ok: bool, latency_ms: float | None, error: str | None) -> None: ...

    def event(self, kind: str, src: str | None, dst: str | None, data: dict[str, Any]) -> None: ...


@dataclass
class Answer:
    value: Any
    tier: str
    attempts: int
    latency_ms: float
    trail: list[dict[str, Any]] = field(default_factory=list)


Check = Callable[[Any, Any], "str | None"]

_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.S)


def parse_json(text: str) -> Any:
    """Parse a model's JSON reply, forgiving code fences and chatter around one JSON object."""
    t = text.strip()
    m = _FENCE.match(t)
    if m:
        t = m.group(1)
    try:
        return json.loads(t)
    except ValueError:
        start, end = t.find("{"), t.rfind("}")
        if start != -1 and end > start:
            return json.loads(t[start:end + 1])
        raise


def _clip(v: Any, n: int = 400) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    return s if len(s) <= n else s[:n] + "…"


class Router:
    def __init__(self, providers: dict[str, Provider], board: Board, *, chain: list[str],
                 attempts_per_tier: int = 2, source: str = "worker", clock: Callable[[], float] = time.perf_counter):
        self.providers = providers
        self.board = board
        self.chain = list(chain)
        self.attempts = attempts_per_tier
        self.source = source  # the component asking (the plugin), for the map
        self.clock = clock
        self.current_tier: str | None = None  # the tier being tried right now (checks may look at it)

    def ask(self, playbook: str, input: Any, *, schema: type[BaseModel] | None = None, check: Check | None = None,
            chain: list[str] | None = None, attempts: int | None = None, images: list[str] | None = None,
            advice: str | None = None, web: bool = False, on_text: Any = None, temperature: float = 0.0) -> Answer:
        """`on_text(text_so_far)`: the first try of the first tier streams its answer there (Ollama only), so the
        start of a reply can be used before it is done; later tries don't (their text would contradict it).
        `images`: base64-encoded pictures for a vision model (sent with the first message).
        `advice`: what went wrong before (another chain already tried), handed to the first tier."""
        tiers = chain or self.chain
        tries = attempts or self.attempts
        if isinstance(schema, dict):  # a plain JSON schema (replaying a stored playbook): answers are parsed JSON
            json_schema = schema
        else:
            json_schema = schema.model_json_schema() if schema is not None else None
        system = playbook.strip()
        if json_schema is not None:
            system += "\n\nReply with only a JSON object that matches the given schema. No extra text."
        task = input if isinstance(input, str) else json.dumps(input, ensure_ascii=False, indent=2, default=str)
        trail: list[dict[str, Any]] = []
        # advice: what the previous tier got wrong, handed up the chain
        prev: str | None = None

        for tier in tiers:
            comp = tier.lower()
            provider = self.providers.get(tier)
            if provider is None:
                trail.append({"tier": tier, "skipped": "not available on this worker"})
                continue
            if prev is not None:
                self.board.event("model.escalated", prev.lower(), comp,
                                 {"from_tier": prev, "to_tier": tier, "reason": _clip(advice or "", 200)})
            self.current_tier = tier
            messages: list[dict[str, Any]] = [{"role": "user", "content": task}]
            if images:
                messages[0]["images"] = list(images)
            if advice:
                messages[0]["content"] += f"\n\nA smaller model tried this first and failed:\n{advice}"
            for attempt in range(1, tries + 1):
                # a permit per call: the breaker may have opened meanwhile, and each Claude call costs budget
                permit = self.board.permit(tier)
                if not permit.get("allowed"):
                    trail.append({"tier": tier, "attempt": attempt, "skipped": permit.get("reason")})
                    self.board.event("model.skipped", self.source, comp,
                                     {"tier": tier, "reason": permit.get("reason")})
                    break
                self.board.event("model.request", self.source, comp, {"tier": tier, "attempt": attempt})
                t0 = self.clock()
                try:
                    extra = {"web": True} if web and getattr(provider, "kind", "") == "claude" else {}
                    if on_text is not None and tier == tiers[0] and attempt == 1 \
                            and getattr(provider, "kind", "") == "ollama":
                        extra["on_text"] = on_text
                    if temperature and getattr(provider, "kind", "") == "ollama":
                        extra["temperature"] = temperature
                    reply = provider.chat(system, messages, json_schema, **extra)
                except ModelError as e:
                    ms = (self.clock() - t0) * 1000
                    self.board.report(tier, False, ms, str(e))
                    kind = "model.timeout" if isinstance(e, ModelTimeout) else "model.error"
                    self.board.event(kind, comp, self.source, {"tier": tier, "error": _clip(str(e), 200)})
                    trail.append({"tier": tier, "attempt": attempt, "error": str(e)})
                    break  # an unreachable model won't improve on a retry; move up the chain
                self.board.report(tier, True, reply.latency_ms, None)
                value, problem = self._judge(reply.text, input, schema, check)
                if problem is None:
                    self.board.event("model.reply", comp, self.source,
                                     {"tier": tier, "attempt": attempt, "ok": True,
                                      "latency_ms": round(reply.latency_ms)})
                    trail.append({"tier": tier, "attempt": attempt, "ok": True})
                    return Answer(value, tier, len(trail), reply.latency_ms, trail)
                self.board.event("check.failed", comp, self.source,
                                 {"tier": tier, "attempt": attempt, "reason": _clip(problem, 200)})
                trail.append({"tier": tier, "attempt": attempt, "rejected": problem, "reply": _clip(reply.text)})
                advice = f"Its answer: {_clip(reply.text)}\nWhy it was rejected: {problem}"
                messages = [*messages, {"role": "assistant", "content": reply.text},
                            {"role": "user", "content": f"That answer was rejected: {problem}\nTry again."}]
            prev = tier

        raise EscalationExhausted(f"no tier could answer ({', '.join(tiers)})", trail)

    @staticmethod
    def _judge(text: str, input: Any, schema: type[BaseModel] | None, check: Check | None) -> tuple[Any, str | None]:
        value: Any = text
        if isinstance(schema, dict):
            try:
                value = parse_json(text)
            except ValueError as e:
                return None, f"not valid JSON ({e})"
        elif schema is not None:
            try:
                value = schema.model_validate(parse_json(text))
            except ValueError as e:  # json errors and ValidationError both subclass ValueError
                first = e.errors()[0] if isinstance(e, ValidationError) else None
                why = (f"{'.'.join(map(str, first['loc']))}: {first['msg']}" if first
                       else f"not valid JSON ({e})")
                return None, f"answer does not match the schema: {why}"
        if check is not None:
            try:
                problem = check(value, input)
            except Exception as e:  # a crashing check is a failed check, not a crashed worker
                problem = f"check crashed: {type(e).__name__}: {e}"
            if problem:
                return None, str(problem)
        return value, None
