"""Ari's conversation thread: kept per chat, folded after every turn, handed back with the next message."""

from __future__ import annotations

import copy
import json

from argus import ari_eval
from argus import thread as th
from argus.ari_conversations import CONVERSATIONS
from argus.worker import Worker
from argus.worker.think import follow_on
from fakes import FakeOllama
from test_ari_think import make, settle
from test_worker import Server, client

NOW = 1_000_000.0


def test_tools_set_the_app_the_topic_and_what_was_mentioned():
    t = th.update(None, message="open spotify", now=NOW,
                  out={"reply": "Opening Spotify.", "used": [{"tool": "open_app", "args": {"name": "spotify"}}]})
    assert t["app"] == "spotify" and t["topic"] == "spotify open" and t["task"]["goal"] == "open spotify"
    t = th.update(t, message="play Famous", now=NOW + 10, out={
        "reply": "Playing Famous.", "used": [{"tool": "play_on_spotify", "args": {"query": "Famous"}}]})
    assert t["app"] == "Spotify" and t["mentioned"]["song"] == "Famous" and t["topic"] == "music on Spotify"
    b = th.brief(t, NOW + 70)
    assert b["app"] == "Spotify" and "music" in b and b["minutes_ago"] == 1
    assert "volume" in th.words(t)


def test_a_question_keeps_the_task_open_until_it_is_answered():
    t = th.update(None, message="message Kaancha on WhatsApp", now=NOW,
                  out={"reply": "Sure. What should I say to her?", "used": []})
    assert t["question"] == "What should I say to her?" and t["task"]["missing"] == t["question"]
    t = th.update(t, message="tell her I'm late", now=NOW + 5, out={
        "reply": "Shall I message Kaancha \"I'm late\" on WhatsApp?",
        "pending": {"kind": "tool", "name": "whatsapp_message"}, "used": []})
    assert t["task"]["goal"] == "message Kaancha on WhatsApp"  # the same task, still waiting for the yes
    t = th.update(t, message="what's the weather", now=NOW + 9, out={
        "reply": "Sunny.", "used": [{"tool": "weather", "args": {}}]})
    assert t["task"]["goal"] == "message Kaancha on WhatsApp" and t["task"]["steps"] == ["weather"]


def test_new_work_starts_a_new_task_and_small_talk_keeps_it():
    t = th.update(None, message="find my cv", now=NOW, out={"reply": "Found it.", "used": [
        {"tool": "search_everything", "args": {"query": "cv"}, "result": [{"path": "C:/Users/Sas/Documents/CV.pdf"}]}]})
    assert t["mentioned"]["file"] == "C:/Users/Sas/Documents/CV.pdf" and t["mentioned"]["search"] == "cv"
    t = th.update(t, message="thanks", now=NOW + 3, out={"reply": "Any time.", "used": []})
    assert t["task"]["goal"] == "find my cv" and not t["question"]
    opened = [{"tool": "open_app", "args": {"name": "chrome"}}]
    t = th.update(t, message="open chrome", now=NOW + 6, out={"reply": "Opening Chrome.", "used": opened})
    assert t["task"]["goal"] == "open chrome" and t["app"] == "chrome"


def test_private_tools_leave_no_details_and_the_thread_lapses():
    t = th.update(None, message="read Kaancha's mail", now=NOW, out={"reply": "She says hi.", "used": [
        {"tool": "read_email", "args": {"which": "from Kaancha"}, "result": "secret", "private": True}]})
    assert "mail" not in t["mentioned"] and "secret" not in json.dumps(t)
    t = th.update(t, message="read the second one", now=NOW, out={"reply": "GitHub.", "used": [
        {"tool": "read_email", "args": {"which": "2"}, "private": True}]})
    assert t["mentioned"]["mail"] == "2"
    assert th.brief(t, NOW + th.FRESH_S + 1) == {} and th.fresh(t, NOW + th.FRESH_S + 1)["topic"] == ""


def test_promises_and_failures_are_kept():
    t = th.update(None, message="remind me", now=NOW, out={"reply": "Done. I'll remind you at 5 pm.", "used": [
        {"tool": "shout", "args": {}, "error": "boom"}]})
    assert t["promises"] == ["remind you at 5 pm"] and t["task"]["failed"] == "shout"


def test_play_while_spotify_is_in_use_goes_straight_to_it():
    tools = {"play_on_spotify": {}, "media_control": {}}
    t = th.update(None, message="open spotify", out={"reply": "Opening Spotify.",
                                                     "used": [{"tool": "open_app", "args": {"name": "Spotify"}}]})
    assert follow_on(tools, "play Famous", t) == ("play_on_spotify", {"query": "Famous"})
    assert follow_on(tools, "play it", t) is None and follow_on(tools, "play Famous on youtube", t) is None
    assert follow_on(tools, "play Famous", None) is None


def test_the_thread_goes_round_trip_through_argusd(tmp_path):
    replies = {"qwen2.5-coder:7b": [
        {"tool": "shout", "args": {"text": "spotify"}}, {"reply": "Shouted.", "topic": "shouting things"},
        {"reply": "Again then."}]}
    with FakeOllama(replies) as ol, Server(make(tmp_path).open()) as srv:
        cl = client(srv.url)
        w = Worker(cl, "pc", capabilities=["desktop", "session"], ollama_url=ol.url, watch_folders=False)
        w.register()
        r = cl.post("/ari", {"text": "shout spotify"})
        settle(cl, w, r["conv"])
        first = json.loads(ol.requests[0]["messages"][1]["content"])
        assert "thread" not in first  # a new chat: nothing to carry
        job = cl.get(f"/jobs/{r['job_id']}")
        assert job["result"]["thread"]["topic"] == "shouting things" and "topic" not in job["result"]
        r2 = cl.post("/ari", {"text": "louder", "conv": r["conv"]})
        settle(cl, w, r2["conv"])
        asked = json.loads(ol.requests[-1]["messages"][1]["content"])
        assert list(asked)[:2] == ["message", "thread"]
        assert asked["thread"]["topic"] == "shouting things" and asked["thread"]["task"]["steps"] == ["shout"]
        assert "thread" in ol.requests[-1]["messages"][0]["content"]  # the playbook explains it


def test_scripted_conversations_score_each_step_in_context():
    tools = [{"name": n, "does": n} for n in ("open_app", "play_on_spotify", "set_volume", "media_control",
                                              "check_email", "read_email", "weather")]
    seen = []

    def decide(task):
        seen.append(task)
        if task["message"] == "louder":
            return {"tool": "set_volume", "args": {"level": 30}}  # the right tool, but "up" was wanted
        return {"reply": "?"}

    convs = copy.deepcopy([c for c in CONVERSATIONS if c["name"] in (
        "spotify: open, play, louder, pause", "mail: check, read the second", "lab, then the backup")])
    convs[0]["steps"][2]["args"]["change"] = "up"  # this test wants the argument checked too
    out = ari_eval.run_conversations(convs, tools, decide, log=lambda *_: None)
    assert out["of"] == 2 and out["right"] == 1 and out["skipped"] == ["lab, then the backup"]
    assert out["steps"] == 6 and out["steps_right"] == 5
    miss = [x for c in out["conversations"] for x in c["steps"] if not x["ok"]][0]
    assert miss["say"] == "louder" and miss["wrong_args"] == ["change"]
    assert [t["message"] for t in seen] == ["louder"]  # the rest go straight to their tool, as in a real chat
    loud = seen[0]
    assert loud["thread"]["app"] == "Spotify" and loud["thread"]["mentioned"]["song"] == "famous"
    assert loud["conversation_so_far"][-1]["found"][0]["tool"] == "play_on_spotify"
    assert "found" not in loud["conversation_so_far"][-3]  # only the last answer carries what was found
    s = ari_eval.summary(out, "qwen")
    assert s["steps_right"] == 5 and s["misses"][0]["say"] == "louder"


def test_every_scripted_conversation_is_well_formed():
    assert len(CONVERSATIONS) >= 30
    for c in CONVERSATIONS:
        assert len(c["steps"]) >= 2 and c["name"]
        for st in c["steps"]:
            assert st["say"] and st["expect"] and st["said"], (c["name"], st["say"])


def test_conversation_scores_are_kept_for_helios(tmp_path):
    with Server(make(tmp_path).open()) as srv:
        cl = client(srv.url)
        cl.post("/ari/conversations", {"model": "qwen", "right": 25, "of": 30, "steps_right": 60, "steps": 68,
                                       "p50": 0.8, "misses": [{"name": "x", "say": "louder", "got": "reply",
                                                               "expect": "set_volume"}]})
        runs = cl.get("/ari/conversations")["runs"]
        assert runs[-1]["right"] == 25 and runs[-1]["misses"][0]["say"] == "louder" and runs[-1]["at"]


def test_argusd_answers_keep_the_thread_too_and_the_listener_note_keeps_it(tmp_path):
    from argus import ari as ari_mod

    argus = make(tmp_path).open()
    with Server(argus) as srv:
        cl = client(srv.url)
        r = cl.post("/ari", {"text": "remember that my sister lives in Kandy"})  # answered by argusd, no model
        assert r["reply"].startswith("Got it")
        cl.post("/ari/spoken", {"conv": r["conv"], "answer_ms": 900, "spoken": True})
        row = argus.store.read_sync(lambda c: c.execute("SELECT meta FROM ari_turns WHERE conv = ? AND role = 'ari'",
                                                        (r["conv"],)).fetchone())
        meta = json.loads(row[0])
        assert meta["answer_ms"] == 900 and isinstance(meta["thread"], dict)
        assert argus.store.read_sync(lambda c: ari_mod.thread(c, r["conv"]))["at"] > 0


def _after(tool, args, said="Done.", result=None, private=False):
    u = {"tool": tool, "args": args, **({"result": result} if result is not None else {}),
         **({"private": True} if private else {})}
    return th.update(None, message="x", out={"reply": said, "used": [u]})


def test_short_follow_ups_repeat_the_last_tool_with_the_one_change():
    tools = {n: {} for n in ("weather", "add_to_list", "phone_timer", "whatsapp_message", "open_file", "show_issue",
                             "read_email")}
    w = _after("weather", {"place": "Kandy"})
    assert w["last"] == {"tool": "weather", "args": {"place": "Kandy"}}
    assert follow_on(tools, "and tomorrow?", w) == ("weather", {"place": "Kandy", "tomorrow": True})
    assert follow_on(tools, "what about Colombo", w) == ("weather", {"place": "Colombo"})
    assert follow_on(tools, "and how are you?", w) is None and follow_on(tools, "what about the backup", w) is None
    shop = _after("add_to_list", {"text": "milk"})
    assert follow_on(tools, "and eggs", shop) == ("add_to_list", {"text": "eggs"})
    assert follow_on(tools, "and you?", shop) is None
    assert follow_on(tools, "make it 15", _after("phone_timer", {"minutes": 10})) == ("phone_timer", {"minutes": 15})
    wa = _after("whatsapp_message", {"to": "Kaancha", "text": "on my way"})
    again = ("whatsapp_message", {"to": "Nimali", "text": "on my way"})
    assert follow_on(tools, "send the same to Nimali", wa) == again
    cv = _after("search_everything", {"query": "cv"}, result=[{"path": "C:/Users/Sas/Documents/CV.pdf"}])
    assert follow_on(tools, "open it", cv) == ("open_file", {"path": "C:/Users/Sas/Documents/CV.pdf"})
    issues = _after("list_issues", {})
    history = [{"role": "ari", "text": "Two open.", "found": [{"tool": "list_issues",
                                                                "result": [{"key": "ACME-7"}, {"key": "ACME-9"}]}]}]
    assert follow_on(tools, "show me the first one", issues, history) == ("show_issue", {"key": "ACME-7"})
    assert follow_on(tools, "the last one", issues, history) == ("show_issue", {"key": "ACME-9"})
    mail = _after("check_email", {}, private=True)
    assert mail["last"] == {"tool": "check_email", "args": {}}
    assert follow_on(tools, "what does Kaancha say", mail) == ("read_email", {"which": "from Kaancha"})


def test_the_words_ari_asked_for_go_to_the_message():
    t = th.update(None, message="message Kaancha on WhatsApp", out={"reply": "What should I say to Kaancha?",
                                                                     "used": []})
    assert follow_on({"whatsapp_message": {}}, "tell her I'm running late", t) == (
        "whatsapp_message", {"to": "Kaancha", "text": "I'm running late"})


def test_reading_list_and_watches_need_no_model():
    from argus.worker.think import straight_to

    tools = {"reading_list": {}, "list_watches": {}}
    assert straight_to(tools, "what's on my reading list") == ("reading_list", {})
    assert straight_to(tools, "what am I watching?") == ("list_watches", {})


def test_the_last_three_follow_ups():
    tools = {n: {} for n in ("open_file", "lab_status", "repo_status")}
    found = [{"tool": "search_everything", "result": [{"path": "C:/Users/Sas/Downloads/invoice-sept.pdf"},
                                                      {"path": "C:/Users/Sas/Downloads/invoice-oct.pdf"}]}]
    history = [{"role": "ari", "text": "Two invoices.", "found": found}]
    t = _after("search_everything", {"query": "invoice"})
    assert follow_on(tools, "open the october one", t, history) == (
        "open_file", {"path": "C:/Users/Sas/Downloads/invoice-oct.pdf"})
    assert follow_on(tools, "open the second one", t, history) == (
        "open_file", {"path": "C:/Users/Sas/Downloads/invoice-oct.pdf"})
    assert follow_on(tools, "open the invoice one", t, history) is None  # both match: the model asks which
    notes = _after("find_notes", {"query": "server"})
    assert follow_on(tools, "is it up?", notes) == ("lab_status", {})
    assert follow_on(tools, "is it up?", _after("find_notes", {"query": "recipes"})) is None
    assert follow_on(tools, "and the argus repo?", _after("what_changed_today", {})) == (
        "repo_status", {"name": "argus"})
