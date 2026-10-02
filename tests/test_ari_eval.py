"""Ari's test set: right tool, how fast (no tool runs)."""

from __future__ import annotations

from argus import ari_eval


def test_the_test_set_scores_ari_first_decision():
    tools = [{"name": n, "does": d} for n, d in [("set_volume", "Change the PC's volume"),
                                                  ("weather", "The weather where you are"),
                                                  ("web_search", "Search the web")]]
    asked = []

    def decide(task):
        asked.append(task["message"])
        return {"tool": "weather"} if "rain" in task["message"] else {"reply": "Sure."}

    out = ari_eval.run([("how are you", "chat"), ("volume 30", "set_volume"), ("will it rain", "weather"),
                        ("latest python release", "web_search|web")], tools, decide, log=lambda *_: None)
    assert out["right"] == 3 and out["of"] == 4
    assert asked == ["will it rain", "latest python release"]  # small talk and direct paths need no model
    assert [c["got"] for c in out["cases"]] == ["chat", "set_volume", "weather", "reply"]


def test_tools_are_matched_by_what_they_do():
    from argus.worker.think import closest

    tools = {f"tool_{i}": {"does": f"thing {i}"} for i in range(30)}
    tools["phone_torch"] = {"does": "Turn the phone's flashlight on or off"}
    assert "phone_torch" in closest(tools, "is the flashlight working")
