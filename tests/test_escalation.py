"""Every plugin: when the local models can't do it, Claude gets one try; then the plugin can ask you."""

from __future__ import annotations

import pytest

from argus.models import EscalationExhausted
from argus.worker.workflows import Context


class Ans:
    def __init__(self, tier, value):
        self.tier, self.value = tier, value


class Router:
    def __init__(self, fail_local=True, fail_claude=False):
        self.chain = ["T1", "T2", "T3"]
        self.providers = {"T1": type("P", (), {"kind": "ollama"})(), "T2": type("P", (), {"kind": "ollama"})(),
                          "T3": type("P", (), {"kind": "claude"})(), "V1": type("P", (), {"kind": "ollama"})()}
        self.calls = []
        self.fail_local, self.fail_claude = fail_local, fail_claude
        self.board = type("B", (), {"event": lambda *a, **k: None})()

    def ask(self, playbook, input, *, chain=None, advice=None, images=None, **kw):
        self.calls.append((list(chain or self.chain), advice, images))
        if chain == ["T3"]:
            if self.fail_claude:
                raise EscalationExhausted("no tier could answer (T3)", [{"tier": "T3", "skipped": "cap"}])
            return Ans("T3", "claude says")
        if self.fail_local:
            raise EscalationExhausted("no tier could answer", [{"tier": (chain or self.chain)[-1], "rejected": "bad",
                                                                "reply": "junk"}])
        return Ans(chain[0], "local says")


class Rep:
    lease_lost = False


def ctx(router) -> Context:
    c = Context({"id": "j", "plugin": "p"}, Rep())
    c._router = router
    return c


def test_claude_after_the_local_models_with_their_mistakes():
    r = Router()
    c = ctx(r)
    assert c.llm("pb", "x", tiers=["V1"], images=[b"\x89PNG"]) == "claude says"
    assert r.calls[1][0] == ["T3"] and "junk" in r.calls[1][1] and r.calls[1][2]  # the picture goes along
    assert c.last_answer.tier == "T3"


def test_no_second_claude_when_claude_was_in_the_chain_or_turned_off():
    r = Router()
    with pytest.raises(EscalationExhausted):
        ctx(r).llm("pb", "x")  # T1, T2, T3 already
    assert len(r.calls) == 1
    r2 = Router()
    with pytest.raises(EscalationExhausted):
        ctx(r2).llm("pb", "x", tiers=["T1"], claude_last=False)
    assert len(r2.calls) == 1


def test_claude_out_of_budget_still_raises_with_both_reasons():
    r = Router(fail_claude=True)
    with pytest.raises(EscalationExhausted) as e:
        ctx(r).llm("pb", "x", tiers=["T1", "T2"])
    assert "T3" in str(e.value) and len(e.value.trail) == 2


def test_local_tiers_leave_claude_out():
    assert ctx(Router()).local_tiers() == ["T1", "T2"]
