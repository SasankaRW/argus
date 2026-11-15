"""Request bodies for the Argus HTTP API (shared by the routers in routes/)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

MAX_CLAIM_WAIT = 30.0


class SubmitJob(BaseModel):
    plugin: str = Field(min_length=1, max_length=100)
    workflow: str = Field(min_length=1, max_length=100)
    input: dict[str, Any] = Field(default_factory=dict)
    needs: list[str] = Field(default_factory=list)
    priority: int = Field(50, ge=0, le=100)
    dedupe_key: str | None = None
    max_attempts: int | None = Field(None, ge=1, le=20)
    delay: float = Field(0, ge=0)
    model: str | None = Field(None, max_length=100)  # groups GPU jobs by model (fewer swaps)
    window: str | None = Field(None, max_length=40)  # only start inside this window (e.g. night)


class InboxAck(BaseModel):
    ids: list[str] = Field(default_factory=list, max_length=100)


class RegisterWorker(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    host: str = Field(min_length=1, max_length=100)
    capabilities: list[str] = Field(default_factory=list)
    version: str | None = None


class Claim(BaseModel):
    capabilities: list[str] = Field(default_factory=list)
    plugins: list[str] | None = None
    wait: float = Field(0, ge=0, le=MAX_CLAIM_WAIT)
    min_priority: int | None = None  # a worker's fast lane: only jobs at least this urgent


class WorkerOnly(BaseModel):
    worker: str


class StepReport(BaseModel):
    worker: str
    idx: int = Field(ge=0)
    name: str
    state: str = Field(pattern="^(running|succeeded|failed)$")
    output: Any = None
    error: str | None = None
    tier: str | None = None


class Succeed(BaseModel):
    worker: str
    result: Any = None


class Fail(BaseModel):
    worker: str
    error: str
    retryable: bool = True


class Wait(BaseModel):
    worker: str
    reason: str


class NewShare(BaseModel):
    title: str = Field("", max_length=300)
    text: str = Field("", max_length=20000)
    url: str = Field("", max_length=2000)
    note: str = Field("", max_length=2000)


class SendShare(BaseModel):
    plugin: str
    workflow: str


class RulesText(BaseModel):
    text: str = Field(max_length=200_000)


class Ask(BaseModel):
    text: str = Field(min_length=1, max_length=500)


class AskDo(BaseModel):
    action: str = Field(min_length=1, max_length=200)


class AriSay(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    conv: str | None = Field(None, pattern=r"^[A-Za-z0-9_-]{1,40}$")
    source: str | None = Field(None, max_length=20)  # voice, helios, phone (the timeline says where it came from)
    heard: dict[str, Any] | None = None  # the listener's notes: the language check, a second listen for "Hey Ari"


class AriAnswer(BaseModel):
    yes: bool


class AriStateIn(BaseModel):
    phase: Literal["idle", "listening", "thinking", "working", "speaking", "done", "following"]
    text: str = Field("", max_length=300)
    by: str = Field("", max_length=40)


class ConvScore(BaseModel):
    """A run of the scripted conversations (python -m argus.ari.ari_eval --conversations)."""
    at: float | None = None
    model: str = Field("", max_length=120)
    right: int = Field(ge=0)
    of: int = Field(ge=0)
    steps_right: int = Field(0, ge=0)
    steps: int = Field(0, ge=0)
    p50: float = 0.0
    misses: list[dict[str, Any]] = Field(default_factory=list, max_length=40)


class AriListening(BaseModel):
    on: bool
    minutes: float | None = Field(None, gt=0, le=24 * 60)  # paused for this long (None: until turned back on)


class AriSpeak(BaseModel):
    text: str = Field(min_length=1, max_length=1000)


class ToolCall(BaseModel):
    worker: str
    key: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=60)
    args: dict[str, Any] = Field(default_factory=dict)


class SampleIn(BaseModel):
    worker: str
    playbook: str = Field(min_length=1, max_length=40_000)
    schema_: dict[str, Any] | None = Field(None, alias="schema")
    input: Any = None
    output: Any = None
    tier: str | None = None
    escalated: bool = False
    subject: str | None = Field(None, max_length=2000)  # what it was about (file names): links your fixes to it
    used: list[int] | None = None  # the examples it was shown (experience memory)


class Replay(BaseModel):
    tier: str = Field(min_length=1, max_length=20)


class Verdict(BaseModel):
    verdict: str | None = Field(None, pattern="^(correct|wrong)$")
    correction: Any = None


class LessonDecide(BaseModel):
    approve: bool


class PluginSettings(BaseModel):
    live: bool | None = None  # None: back to argus.yaml's plugins.live
    config: dict[str, Any] | None = None  # only the fields you changed; a null value resets that field
    reset: bool = False  # forget all your changes


class ClaudeAuth(BaseModel):
    logged_in: bool
    detail: str = Field("", max_length=300)


class ScheduleEdit(BaseModel):
    enabled: bool


class Fix(BaseModel):
    value: str = Field(min_length=1, max_length=200)  # the right answer, from the Wrong button's choices


class TraceEvent(BaseModel):
    worker: str
    kind: str = Field(min_length=3, max_length=60)
    src: str | None = Field(None, max_length=100)
    dst: str | None = Field(None, max_length=100)
    step: str | None = Field(None, max_length=100)
    data: dict[str, Any] | None = None


class RunPlugin(BaseModel):
    workflow: str | None = Field(None, max_length=100)  # default: the plugin's first manual trigger
    input: dict[str, Any] = Field(default_factory=dict)


class StateValue(BaseModel):
    value: Any = None


class FileTrigger(BaseModel):
    worker: str = Field(min_length=1, max_length=100)
    trigger: str = Field(min_length=1, max_length=63)
    path: str = Field(min_length=1, max_length=2000)
    sha256: str = Field(min_length=64, max_length=64)
    size: int = Field(ge=0)


class AskApproval(BaseModel):
    worker: str
    key: str = Field(min_length=1, max_length=200)
    type: str = Field(pattern="^(entry|batch|draft)$")
    title: str = Field(min_length=1, max_length=200)
    fields: dict[str, Any] = Field(default_factory=dict)
    items: list[dict[str, Any]] = Field(default_factory=list, max_length=5000)
    summary: list[str] = Field(default_factory=list, max_length=50)
    link: str | None = Field(None, max_length=2000)
    step: str | None = Field(None, max_length=100)
    # a small picture to decide by (ctx.ask_me: the screenshot it couldn't name), as a data: URL
    image: str | None = Field(None, max_length=400_000, pattern=r"^data:image/(png|jpeg|webp);base64,[A-Za-z0-9+/=]+$")


class Notify(BaseModel):
    worker: str
    key: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=200)
    text: str = Field("", max_length=3500)
    priority: str = "default"
    tags: list[str] = Field(default_factory=list, max_length=5)
    link: str | None = Field(None, max_length=2000)


class Decide(BaseModel):
    answer: str | None = Field(None, pattern="^(approve|reject)$")
    fields: dict[str, Any] | None = None
    by: str | None = Field(None, max_length=60)


class ModelCall(BaseModel):
    worker: str | None = None
    job_id: str | None = None


class ModelResult(BaseModel):
    worker: str | None = None
    job_id: str | None = None
    ok: bool
    latency_ms: float | None = Field(None, ge=0)
    error: str | None = Field(None, max_length=2000)
