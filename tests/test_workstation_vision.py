"""Ari's Workstation, part 4: numbered marks on a window's picture, the vision model's step, clicks as window
messages, the surface ladder, and the vision model test."""

from __future__ import annotations

import io

import pytest

from argus import vision_test
from argus.worker import browser as br
from argus.worker import marks, winmsg
from test_workstation import FakeDesk, ws
from test_workstation_browser import ctx, plug_ctx

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402


def png(w=200, h=100, colour=(255, 255, 255)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (w, h), colour).save(out, format="PNG")
    return out.getvalue()


def test_numbered_boxes_are_drawn_on_the_picture():
    got = Image.open(io.BytesIO(marks.draw_marks(png(), [{"n": 1, "x": 40, "y": 30, "w": 60, "h": 40},
                                                         {"n": 2, "x": 500, "y": 5, "w": 9, "h": 9}])))
    assert got.size == (200, 100)
    assert got.getpixel((40, 50)) != (255, 255, 255)  # the box's left edge
    assert got.getpixel((70, 50)) == (255, 255, 255)  # inside it: the window still shows


def test_screen_and_picture_points():
    win = (100, 50, 900, 650)
    box = marks.to_picture((300, 150, 400, 200), win, scale=0.5)
    assert box == {"x": 100, "y": 50, "w": 50, "h": 25}
    assert marks.to_screen(100, 50, win, scale=0.5) == (300, 150)
    small, s = marks.shrink(png(2560, 1440), 1280)
    assert s == 0.5 and Image.open(io.BytesIO(small)).size == (1280, 720)
    assert marks.shrink(png(), 1280)[1] == 1.0
    assert winmsg.lparam(10, 20) == (20 << 16) | 10 and winmsg.lparam(-1, 0) == 0xFFFF


class Seen:
    """A window looked at: its picture, a few boxes."""

    sees = True

    def __init__(self, items):
        self.items, self.did = items, []

    def snapshot(self):
        return {"url": "window: Old app", "title": "Old app", "text": "", "items": self.items,
                "picture": {"width": 400, "height": 300}}

    def image(self):
        return b"PICTURE"

    def act(self, step):
        self.did.append(step)
        return {"ok": True}


def test_the_vision_model_picks_a_box_or_a_point_inside_the_picture():
    s = Seen([{"n": 1, "role": "button", "name": "OK"}])
    c = ctx([{"do": "click", "x": 120, "y": 80}, {"do": "click", "n": 1}, {"do": "done", "answer": "ok"}])
    seen_kw = []
    inner = c.llm

    def llm(playbook, task, **kw):
        seen_kw.append(kw)
        return inner(playbook, task, **kw)

    c.llm = llm
    out = br.run_task(c, s, "press the gear icon, then OK", q="", recipe_key="old.app")
    assert out["done"] and [d.get("x") for d in s.did] == [120, None]
    assert seen_kw[0]["tiers"] == ["V1"] and seen_kw[0]["images"] == [b"PICTURE"]
    assert seen_kw[0]["claude_last"] is False  # pictures never go to Claude
    assert [st["name"] for st in c.store.data["recipes"]["old.app"]["steps"]] == ["OK"]  # positions aren't kept
    with pytest.raises(AssertionError, match="inside the picture"):
        br.run_task(ctx([{"do": "click", "x": 999, "y": 10}]), Seen([]), "x")


def test_an_app_with_few_controls_is_looked_at():
    d = FakeDesk([(1, "Word", "winword", "you"), (8, "Old App", "oldapp", "ari")])
    looked = []

    class FewControls:
        def snapshot(self):
            return {"items": [{"n": 1, "role": "button", "name": "OK"}]}

    def vision(hwnd, uia):
        looked.append((hwnd, uia))
        return Seen([])

    c = plug_ctx(d, None, {"app": "old app", "goal": "click the gear"}, surface_for=lambda h: FewControls(),
                 vision_for=vision, decisions=[{"do": "done", "answer": "nothing to do"}])
    assert ws.in_app(c)["done"] and looked and looked[0][0] == 8


def test_which_vision_model_finds_the_button(tmp_path):
    (tmp_path / "a.png").write_bytes(png(1000, 500))
    (tmp_path / "cases.yaml").write_text("- {image: a.png, goal: the search box, box: [100, 50, 300, 100]}\n"
                                         "- {image: a.png, goal: the close button, box: [950, 0, 1000, 40]}\n")
    answers = {"qwen": ['{"x": 200, "y": 70}', '{"x": 10, "y": 10}'],
               "tars": ["click(start_box='(200,150)')", "click(start_box='(975,40)')"]}

    def asker(url, model, png_, goal, w, h, thousand):
        return answers[model].pop(0)

    res = vision_test.run(tmp_path, ["qwen", "tars@thousand"], "http://x", asker=asker, log=lambda *_: None)
    assert (res["qwen"]["hits"], res["tars@thousand"]["hits"]) == (1, 2)
    assert vision_test.point_of("Action: click(start_box='(500,500)')", 800, 600, True) == (400, 300)
    assert vision_test.point_of("nothing", 1, 1, False) is None


# ---------------------------------------------------------------- part 5: asking you, stuck, the real mouse

def test_the_island_shows_the_question_and_answers_it():
    from argus.ari_popup import ask_parts, decision

    p = ask_parts({"approval": "A1", "title": "Ari needs something to go on", "fields": ["answer", "remember"],
                   "summary": ["book a table", "Which time?", "Choices: 7 pm, 8 pm", "Tick remember to ..."]})
    assert p["choices"] == ["7 pm", "8 pm"] and p["answer"] and p["remember"] and not p["confirm"]
    assert "Choices: 7 pm, 8 pm" not in p["lines"]
    assert decision(p, "8 pm", remember=True) == {"answer": "approve", "fields": {"answer": "8 pm", "remember": True},
                                                  "by": "island"}
    assert decision(p, "", yes=True)["answer"] == "reject" and decision(p, "x", yes=False)["answer"] == "reject"
    c = ask_parts({"approval": "A2", "title": "Ari wants to press \"Buy now\"", "fields": ["page"], "summary": []})
    assert c["confirm"] and decision(c, None) == {"answer": "approve", "by": "island"}
    assert decision(c, None, yes=False)["answer"] == "reject"


def test_what_ari_says_when_it_needs_you():
    from argus.ari_listen import ask_text

    assert ask_text({"title": "Ari needs something to go on", "summary": ["x", "Which time?"]}) == \
        "Quick question: Which time? It's on the island."
    assert ask_text({"title": "Ari wants to press \"Buy now\""}) == "Can I press \"Buy now\"? Yes or no on the island."
    assert "stuck" in ask_text({"title": "Ari is stuck"})


class Flaky(Seen):
    """Clicks that do nothing (an app that ignores Ari's clicks)."""

    def __init__(self, items, idle=0.0):
        super().__init__(items)
        self.idle = idle

    def act(self, step):
        self.did.append(step)
        return {"ok": step["do"] == "mouse", **({} if step["do"] == "mouse" else {"error": "nothing happened"})}

    def idle_seconds(self):
        return self.idle

    def picture(self):
        return b"JPEG"


def test_stuck_twice_ari_asks_you_with_a_picture_and_keeps_your_hint():
    s = Flaky([{"n": 1, "role": "button", "name": "Gear"}])
    c = ctx([{"do": "click", "n": 1}, {"do": "click", "n": 1}, {"do": "done", "answer": "ok"}])
    asked = []
    c.ask_me = lambda title, fields, **k: asked.append((title, k.get("image"))) or {"answer": "it's under Tools"}
    c.job_id = "J9"
    tools = []
    c.tool = lambda name, args: tools.append((name, args)) or {}
    out = br.run_task(c, s, "open the settings")
    assert out["done"] and asked == [("Ari is stuck", b"JPEG")]
    assert c.store.data["hints"] == {"app:old app": ["it's under Tools"]}
    assert tools == [("forget_task_pictures", {"job": "J9"})]  # the picture is gone once the task ends
    c2 = ctx([{"do": "done", "answer": "ok"}], store=c.store.data)
    br.run_task(c2, Flaky([]), "open the settings")
    assert c2.asked[0]["earlier_hints"] == ["it's under Tools"]


APP = ("click", "type", "select", "press", "scroll", "mouse", "done", "ask")


def test_the_real_mouse_only_after_your_yes_or_when_you_are_away():
    s = Flaky([{"n": 1, "role": "button", "name": "Gear"}])
    c = ctx([{"do": "mouse", "n": 1}], approve=False)
    out = br.run_task(c, s, "click the gear", allow=APP)
    assert not out["done"] and "mouse" in out["problem"] and s.did == []
    s2 = Flaky([{"n": 1, "role": "button", "name": "Gear"}], idle=60)
    c2 = ctx([{"do": "mouse", "n": 1}, {"do": "done", "answer": "ok"}], approve=False)
    # you are away: no asking
    assert br.run_task(c2, s2, "click the gear", allow=APP)["done"] and s2.did[0]["do"] == "mouse"
    s3 = Flaky([{"n": 1, "role": "button", "name": "Buy"}], idle=60)
    out = br.run_task(ctx([{"do": "mouse", "n": 1}], approve=False), s3, "buy", allow=APP)
    assert not out["done"] and s3.did == []  # buying still asks, mouse or not


def test_the_mouse_shows_the_workstation_and_comes_back():
    d = FakeDesk([(1, "Word", "winword", "you"), (8, "Old App", "oldapp", "ari")])
    went = []
    d.go = lambda desk: went.append(desk)
    clicked = []
    ws.mouse_around(d, 8, sleep=lambda s: None)(lambda: clicked.append(d.fg))
    assert went == ["ari", "you"] and clicked == [8]


def test_a_tasks_pictures_are_dropped_when_it_ends(tmp_path):
    import json

    from argus.approvals import drop_pictures
    from test_learning_p4 import _db

    c = _db(tmp_path)
    c.execute("PRAGMA foreign_keys = OFF")
    for aid, payload in (("A1", {"summary": ["stuck"], "image": "data:image/jpeg;base64,xx"}),
                         ("A2", {"summary": ["no picture"]})):
        c.execute("INSERT INTO approvals (id, job_id, key, plugin, type, title, payload, state, expires_at,"
                  " created_at, updated_at) VALUES (?, 'J1', ?, 'workstation', 'entry', 't', ?, 'approved', 9, 1, 1)",
                  (aid, aid, json.dumps(payload)))
    assert drop_pictures(c, "J1") == 1
    left = {r[0]: json.loads(r[1]) for r in c.execute("SELECT id, payload FROM approvals")}
    assert left["A1"] == {"summary": ["stuck"]} and left["A2"] == {"summary": ["no picture"]}
