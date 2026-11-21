"""Ari's voice conversation, one turn at a time: a short pause does not end what you say, words said while Ari
thinks extend the question, asks never overlap, and talking over Ari stops it. No sound, no models: a made-up
clock, transcripts, and an `ask` the test releases."""

from __future__ import annotations

import threading

from argus.voice import voice_live as vl
from test_voice_live import DT, Clock, F, wake_rest
from test_worker import wait_for


class Player:
    def __init__(self):
        self.said: list[str] = []
        self.log: list[str] = []
        self.busy, self.text = False, ""

    def say(self, parts):
        self.said.append(" ".join(parts))

    def add(self, parts):
        self.said.append(" ".join(parts))

    def pause(self):
        self.log.append("pause")

    def resume(self):
        self.log.append("resume")

    def stop(self):
        self.log.append("stop")
        self.busy = False


class Scene:
    """A Talk whose `ask` blocks until the test sets the question's gate; every ask is recorded."""

    def __init__(self, replies: dict[str, str], gap: float = vl.JOIN_GAP_S, stream: set | None = None):
        self.replies, self.stream = replies, stream or set()
        self.gates = {q: threading.Event() for q in replies}
        self.asked: list[str] = []
        self.saved: list[str] = []  # replies stored when each ask was sent (what its history would hold)
        self.seen: list[list[str]] = []
        self.cancelled = 0
        self.heard: list[str] = []
        self.player = Player()
        self.clock = Clock()
        self.jobs: list[threading.Thread] = []
        self.talk = vl.Talk(turns=vl.Turns(), transcribe=lambda a: self.heard.pop(0), wake_rest=wake_rest,
                            ask=self.ask, player=self.player, clock=self.clock, ask_async=True, run=self.run,
                            join_gap_s=gap, cancel=self.cancel)

    def run(self, f):
        t = threading.Thread(target=f, daemon=True)
        self.jobs.append(t)
        t.start()

    def cancel(self):
        self.cancelled += 1

    def ask(self, q, partial=None):
        self.asked.append(q)
        self.seen.append(list(self.saved))
        if q in self.stream and partial is not None:
            partial(self.replies[q].split(". ")[0] + ".", "neutral")
        self.gates[q].wait(5)
        self.saved.append(self.replies[q])  # the turn is stored when the ask returns
        return {"reply": self.replies[q]}

    def say(self, text: str, at: float | None = None):
        """You say something and stop (the end of speech is heard at `at`)."""
        self.heard.append(text)
        if at is not None:
            self.clock.t = at
        for _ in range(round(1.0 / DT)):
            self.talk.frame(F, 0.95)
        for _ in range(round(1.0 / DT)):
            self.talk.frame(F, 0.05)

    def at(self, t: float):
        self.clock.t = t
        self.talk.tick()

    def finish(self):
        for g in self.gates.values():
            g.set()
        for j in self.jobs:
            j.join(5)


def test_a_short_pause_does_not_end_the_turn():
    s = Scene({"yeah read it": "Reading it."})
    s.gates["yeah read it"].set()
    s.say("Hey Ari, yeah", at=10.0)
    s.at(10.2)  # inside the gap: nothing asked yet
    assert s.asked == [] and s.talk.waiting
    s.say("read it", at=10.3)  # "Yeah" and "read it" were one sentence
    s.at(10.6)
    assert s.asked == []
    s.at(11.2)
    wait_for(lambda: s.player.said)
    assert s.asked == ["yeah read it"]
    assert s.player.said == ["Reading it."]
    s.finish()


def test_a_normal_turn_is_asked_once_the_gap_has_passed_not_before():
    s = Scene({"what time is it": "Nine."})
    s.say("Hey Ari, what time is it", at=5.0)
    assert s.asked == []
    s.at(5.0 + vl.JOIN_GAP_S - 0.9)  # the pause before the end counted: asks a little after the turn ended
    assert s.asked == []
    s.at(5.0 + vl.JOIN_GAP_S)
    wait_for(lambda: s.asked)
    s.finish()
    assert s.asked == ["what time is it"]


def test_speaking_while_ari_thinks_asks_once_with_both_and_the_old_answer_is_never_spoken():
    # "Hi. Hi" then "Then hear it" two seconds later, before the first answer was ready
    s = Scene({"Hi. Hi": "Hey there! Two hi's in a row?", "Hi. Hi Then hear it": "Okay, here it is."})
    s.say("Hey Ari, Hi. Hi", at=0.0)
    s.at(1.3)
    wait_for(lambda: s.asked == ["Hi. Hi"])
    s.say("Then hear it", at=3.0)
    assert s.cancelled == 1
    s.at(4.5)
    wait_for(lambda: len(s.asked) == 2)
    s.gates["Hi. Hi"].set()  # the old one finishes late: ignored
    s.gates["Hi. Hi Then hear it"].set()
    s.finish()
    wait_for(lambda: s.player.said)
    assert s.asked == ["Hi. Hi", "Hi. Hi Then hear it"]
    assert s.player.said == ["Okay, here it is."]
    assert not any("Oh, and about" in x for x in s.player.said)


def test_once_an_answer_is_being_said_the_next_words_wait_for_it():
    s = Scene({"just gonna stay here": "Sure. Want company?", "just gonna stay here is there a plan": "No plan. Rest."},
              stream={"just gonna stay here"})
    s.say("Hey Ari, just gonna stay here", at=0.0)
    s.at(1.3)
    wait_for(lambda: s.player.said == ["Sure."])  # it started to be said
    s.say("is there a plan", at=3.0)
    s.at(4.5)
    # the reply was already being spoken: no new ask until it is saved
    assert s.asked == ["just gonna stay here"]
    s.gates["just gonna stay here"].set()
    wait_for(lambda: len(s.asked) == 2)
    s.gates["just gonna stay here is there a plan"].set()
    s.finish()
    assert s.asked[1] == "is there a plan"
    assert s.cancelled == 0
    assert s.seen[1] == ["Sure. Want company?"]  # the second ask saw the first reply


def test_asks_are_strictly_one_after_the_other_and_the_next_sees_the_previous_reply():
    s = Scene({"what is the plan": "Gym at six.", "and tomorrow": "Rest day."}, stream={"what is the plan"})
    s.say("Hey Ari, what is the plan", at=0.0)
    s.at(1.3)
    wait_for(lambda: s.player.said)  # its first sentence is being said: this question is no longer extendable
    s.say("and tomorrow", at=3.0)
    s.at(4.5)
    assert s.asked == ["what is the plan"]  # waits for the reply to be saved
    s.gates["what is the plan"].set()
    wait_for(lambda: len(s.asked) == 2)
    s.gates["and tomorrow"].set()
    s.finish()
    assert s.asked == ["what is the plan", "and tomorrow"]
    assert s.seen == [[], ["Gym at six."]]
    assert s.cancelled == 0
    assert s.player.said[-1] == "Rest day."


def test_talking_over_ari_stops_it_and_the_words_are_the_next_turn():
    s = Scene({"tell me a story": "Once upon a time. The end.", "no tell me the weather": "Sunny."},
              stream={"tell me a story"})
    s.say("Hey Ari, tell me a story", at=0.0)
    s.at(1.3)
    wait_for(lambda: s.player.said)  # it started talking
    s.player.busy = True
    s.heard.append("no tell me the weather")
    for _ in range(round(1.0 / DT)):  # you talk over it
        s.talk.frame(F, 0.95)
    assert s.player.log[-1] == "pause"
    s.clock.t = 3.0
    for _ in range(round(1.0 / DT)):
        s.talk.frame(F, 0.05)
    assert s.player.log[-2:] == ["pause", "stop"]
    s.at(5.0)
    assert s.asked == ["tell me a story"]  # the muted reply is still being finished and saved first
    s.gates["tell me a story"].set()
    wait_for(lambda: len(s.asked) == 2)
    s.gates["no tell me the weather"].set()
    s.finish()
    assert s.asked == ["tell me a story", "no tell me the weather"]
    assert s.seen[1] == ["Once upon a time. The end."]
    assert s.player.said[0].startswith("Once")
    assert s.player.said[-1] == "Sunny."
    assert "The end." not in " ".join(s.player.said)  # what was left of the old reply was not said


def test_a_backchannel_over_ari_does_not_interrupt():
    s = Scene({"tell me a story": "Once upon a time."})
    s.gates["tell me a story"].set()
    s.say("Hey Ari, tell me a story", at=0.0)
    s.at(1.3)
    wait_for(lambda: s.player.said)
    s.player.busy = True
    s.heard.append("mm")
    for _ in range(round(1.0 / DT)):
        s.talk.frame(F, 0.95)
    for _ in range(round(1.0 / DT)):
        s.talk.frame(F, 0.05)
    assert s.player.log[-1] == "resume" and "stop" not in s.player.log
    s.finish()
    assert s.asked == ["tell me a story"]


def test_stop_clears_the_waiting_question():
    s = Scene({"what time is it": "Nine."})
    s.say("Hey Ari, what time is it", at=0.0)
    assert s.talk.waiting
    s.talk.drop_pending()
    s.at(5.0)
    assert s.asked == [] and not s.talk.waiting


def test_joined():
    assert vl.joined("Yeah.", "Read it") == "Yeah. Read it"
    assert vl.joined("", "Read it") == "Read it"
