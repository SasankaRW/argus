"""Built-in demo plugin, so a fresh install has something to run.

    POST /jobs {"plugin": "demo", "workflow": "echo",  "input": {"text": "hi"}}
    POST /jobs {"plugin": "demo", "workflow": "sleep", "input": {"seconds": 2, "steps": 3}}
    POST /jobs {"plugin": "demo", "workflow": "fail",  "input": {"permanent": false}}
"""

from __future__ import annotations

import time

from .workflows import Context, PermanentError, workflow


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
