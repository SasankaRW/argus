"""Ari's browser (P7 part 2): a task done step by step in the page, the guard rails, recipes, and Spotify. A fake
browser and a scripted model stand in for Chrome and Ollama."""

from __future__ import annotations

from types import SimpleNamespace

from argus.worker import browser as br
from argus.worker.think import straight_to
from test_workstation import FakeDesk, ws


class FakeBrowser:
    """Pages as lists of items; acting on an item may lead to another page."""

    def __init__(self, pages: dict[str, dict], start: str):
        self.pages, self.at = pages, start
        self.did: list[dict] = []
        self.went: list[str] = []

    def snapshot(self):
        p = self.pages[self.at]
        return {"url": self.at, "title": p["title"], "text": p.get("text", ""), "items": p["items"]}

    def goto(self, url, new_tab=False):
        self.went.append(url)
        self.at = url if url in self.pages else self.at
        return {"url": self.at, "title": self.pages[self.at]["title"]}

    def act(self, step):
        self.did.append({k: v for k, v in step.items() if v not in ("", None, [], False)})
        nxt = self.pages[self.at].get("next", {}).get(step.get("n"))
        if step.get("n") is not None and step["do"] in ("click", "type") and \
                step["n"] not in {i["n"] for i in self.pages[self.at]["items"]}:
            return {"ok": False, "error": "no such element"}
        if nxt:
            self.at = nxt
        return {"ok": True, "url": self.at, "title": self.pages[self.at]["title"]}

    def run(self, fn):
        return fn(self)


def ctx(decisions: list[dict], approve: bool = True, answer: str | None = "", store=None):
    st = dict(store or {})
    asked: list = []

    def llm(playbook, task, schema=None, check=None, **kw):
        d = schema(**decisions.pop(0))
        problem = check(d, task) if check else None
        assert problem is None, problem
        asked.append(task)
        return d

    return SimpleNamespace(
        input={}, dry_run=False, config={"never_touch": ["directfn"]}, asked=asked, approvals=[],
        store=SimpleNamespace(get=lambda k, d=None: st.get(k, d), set=lambda k, v: st.__setitem__(k, v), data=st),
        step=lambda name, fn, *a, **k: fn(*a, **k), llm=llm, local_tiers=lambda: ["T1"],
        approve=lambda *a, **k: (asked.append(("approve", a, k)), approve)[1],
        ask_me=lambda *a, **k: (asked.append(("ask", a, k)), None if answer is None else {"answer": answer})[1])


SITE = {
    "https://shop.example/": {"title": "Shop", "items": [
        {"n": 1, "role": "textbox", "name": "Search", "type": "text"},
        {"n": 2, "role": "button", "name": "Go"}], "next": {2: "https://shop.example/r"}},
    "https://shop.example/r": {"title": "Results for kettle", "text": "Kettle 2L - Rs 5,400", "items": [
        {"n": 1, "role": "link", "name": "Kettle 2L"},
        {"n": 2, "role": "button", "name": "Buy now"}], "next": {2: "https://shop.example/done"}},
    "https://shop.example/done": {"title": "Order placed", "items": []},
}


def test_a_task_step_by_step_and_its_answer():
    b = FakeBrowser(SITE, "https://shop.example/")
    c = ctx([{"do": "type", "n": 1, "text": "kettle"}, {"do": "click", "n": 2},
             {"do": "done", "answer": "The 2 litre kettle is Rs 5,400."}])
    out = br.run_task(c, b, "find the price of a kettle on shop.example")
    assert out["done"] and "5,400" in out["answer"] and out["steps"] == 2
    assert [d["do"] for d in b.did] == ["type", "click"]
    seen = c.asked[2]  # the third look: the results page, numbered
    assert seen["you_can_use"] == ['[1] link "Kettle 2L"', '[2] button "Buy now"']
    assert seen["done_so_far"][0]["on"] == "Search"


def test_buying_waits_for_your_yes():
    b = FakeBrowser(SITE, "https://shop.example/r")
    c = ctx([{"do": "click", "n": 2}], approve=False)
    out = br.run_task(c, b, "buy the kettle")
    assert not out["done"] and "you said no" in out["problem"] and b.did == []
    assert c.asked[-1][0] == "approve" and "Buy now" in c.asked[-1][1][1]
    b2 = FakeBrowser(SITE, "https://shop.example/r")
    out = br.run_task(ctx([{"do": "click", "n": 2}, {"do": "done", "answer": "Ordered."}]), b2, "buy the kettle")
    assert out["done"] and b2.at == "https://shop.example/done"


def test_passwords_and_card_numbers_are_yours_to_type():
    login = {"https://bank.example/": {"title": "Sign in", "items": [
        {"n": 1, "role": "textbox", "name": "Username"},
        {"n": 2, "role": "textbox", "name": "", "type": "password"},
        {"n": 3, "role": "textbox", "name": "Card number", "auto": "cc-number"}]}}
    for n in (2, 3):
        b = FakeBrowser(login, "https://bank.example/")
        out = br.run_task(ctx([{"do": "type", "n": n, "text": "hunter2"}]), b, "pay the bill")
        assert out["handover"] and not out["done"] and b.did == []
    assert br.secret_field({"name": "One-time code"}) and not br.secret_field({"name": "Search"})


def test_what_it_doesnt_know_it_asks():
    form = {"https://form.example/": {"title": "Contact",
                                      "items": [{"n": 1, "role": "textbox", "name": "Order number"}]}}
    b = FakeBrowser(form, "https://form.example/")
    c = ctx([{"do": "ask", "question": "What's the order number?"}, {"do": "type", "n": 1, "text": "A-1234"},
             {"do": "done", "answer": "Filled in."}], answer="A-1234")
    out = br.run_task(c, b, "fill the contact form")
    assert out["done"] and b.did[0]["text"] == "A-1234"
    kind, args, kw = c.asked[1]
    assert kind == "ask" and args[1] == {"answer": "", "remember": False}
    assert kw["summary"][:2] == ["fill the contact form", "What's the order number?"]
    assert c.asked[2]["user_answers"] == [{"question": "What's the order number?", "answer": "A-1234"}]
    b2 = FakeBrowser(form, "https://form.example/")
    out = br.run_task(ctx([{"do": "ask", "question": "Which?"}], answer=None), b2, "fill it")
    assert not out["done"] and "didn't answer" in out["problem"]


def test_a_task_that_worked_is_replayed_without_the_model():
    store: dict = {}
    b = FakeBrowser(SITE, "https://shop.example/")
    c = ctx([{"do": "type", "n": 1, "text": "kettle", "enter": False}, {"do": "click", "n": 2},
             {"do": "done", "answer": "ok"}], store=store)
    assert br.run_task(c, b, "search kettle", q="kettle", recipe_key="shop.search")["done"]
    steps = c.store.data["recipes"]["shop.search"]["steps"]
    assert steps[0]["role"] == "textbox" and steps[0]["text"] == "{q}" and steps[1]["name"] == "Go"
    b2 = FakeBrowser(SITE, "https://shop.example/")
    c2 = ctx([], store=c.store.data)  # no model answers at all
    out = br.run_task(c2, b2, "search toaster", q="toaster", recipe_key="shop.search",
                      check=lambda: {"ok": b2.at.endswith("/r")})
    assert out == {"done": True, "answer": "done the usual way", "via": "recipe"} and b2.did[0]["text"] == "toaster"
    # it stops working twice: dropped, and the model works it out again
    for _ in range(2):
        b3 = FakeBrowser(SITE, "https://shop.example/")
        c3 = ctx([{"do": "done", "answer": "gave up"}], store=c.store.data)
        br.run_task(c3, b3, "search toaster", q="toaster", recipe_key="shop.search", check=lambda: {"ok": False})
    assert "shop.search" not in c.store.data["recipes"]


def test_the_page_cannot_send_ari_to_local_addresses():
    assert br.safe_url("https://example.com") and not br.safe_url("http://127.0.0.1:8600/jobs")
    assert not br.safe_url("http://192.168.1.1/admin") and not br.safe_url("file:///C:/x")


# ---------------------------------------------------------------- the Workstation plugin


def plug_ctx(desk, b, inp, **extra):
    c = ctx(extra.pop("decisions", []))
    c.input, c.desk, c.browser, c.browser_pids = inp, desk, b, {77}
    c.config = {"never_touch": ["directfn"], "browser": "chrome", "spotify": "web"}  # (the app route: test_workstation)
    for k, v in extra.items():
        setattr(c, k, v)
    return c


def test_spotify_the_direct_route():
    d = FakeDesk([(1, "Word", "winword", "you")])
    d.wins[50] = ws.Win(50, "Spotify - Web Player", "chrome", 77)
    d.on[50] = "you"  # Ari's browser opened where you are
    b = FakeBrowser({"x": {"title": "Spotify", "items": []}}, "x")
    c = plug_ctx(d, b, {"query": "bohemian rhapsody"},
                 spotify_first=lambda page: {"ok": True, "playing": "Bohemian Rhapsody"})
    out = ws.spotify(c)
    assert out == {"done": True, "playing": "Bohemian Rhapsody"}
    assert b.went == ["https://open.spotify.com/search/bohemian%20rhapsody/tracks"]
    assert d.on[50] == "ari" and c.store.data["last"]["hwnd"] == 50


def test_spotify_wants_you_to_sign_in_once():
    d = FakeDesk([(1, "Word", "winword", "you")])
    d.wins[50] = ws.Win(50, "Spotify - Web Player", "chrome", 77)
    d.on[50] = "ari"
    b = FakeBrowser({"x": {"title": "Spotify", "items": []}}, "x")
    out = ws.spotify(plug_ctx(d, b, {"query": "x"}, spotify_first=lambda page: {"ok": False, "login": True}))
    assert out["handed_to_you"] and "sign in" in out["problem"] and d.on[50] == "you" and d.fg == 50


def test_a_task_hands_the_window_over_for_a_password():
    d = FakeDesk([(1, "Word", "winword", "you")])
    d.wins[50] = ws.Win(50, "Sign in - Google Chrome", "chrome", 77)
    d.on[50] = "ari"
    b = FakeBrowser({"https://bank.example/": {"title": "Sign in", "items": [
        {"n": 1, "role": "textbox", "name": "Password", "type": "password"}]}}, "https://bank.example/")
    c = plug_ctx(d, b, {"goal": "log in to my bank", "url": "https://bank.example/"},
                 decisions=[{"do": "type", "n": 1, "text": "x"}])
    out = ws.do_in_browser(c)
    assert out["handed_to_you"] and d.on[50] == "you"


def test_play_on_spotify_said_plainly():
    tools = {t: {} for t in ("play_on_spotify", "media_control")}
    assert straight_to(tools, "play bohemian rhapsody on spotify") == ("play_on_spotify",
                                                                      {"query": "bohemian rhapsody"})
    assert straight_to(tools, "put on some lofi beats on Spotify please") == ("play_on_spotify",
                                                                             {"query": "lofi beats"})
    assert straight_to(tools, "play") == ("media_control", {"action": "play_pause"})


def test_details_you_ask_it_to_remember_are_used_next_time_but_never_secrets():
    form = {"https://form.example/": {"title": "Contact",
                                      "items": [{"n": 1, "role": "textbox", "name": "Email"}]}}
    c = ctx([{"do": "ask", "question": "What's your email?"}, {"do": "type", "n": 1, "text": "sas@example.com"},
             {"do": "done", "answer": "ok"}])
    c.ask_me = lambda *a, **k: {"answer": "sas@example.com", "remember": True}
    br.run_task(c, FakeBrowser(form, "https://form.example/"), "fill the form")
    assert c.store.data["details"] == {"What's your email?": "sas@example.com"}
    c2 = ctx([{"do": "done", "answer": "ok"}], store=c.store.data)
    br.run_task(c2, FakeBrowser(form, "https://form.example/"), "fill the form")
    assert c2.asked[0]["known_details"] == {"What's your email?": "sas@example.com"}
    c3 = ctx([{"do": "ask", "question": "What's your card number?"}, {"do": "done", "answer": "ok"}])
    asked = []
    c3.ask_me = lambda *a, **k: asked.append(a[1]) or {"answer": "4111111111111111", "remember": True}
    br.run_task(c3, FakeBrowser(form, "https://form.example/"), "pay")
    assert asked == [{"answer": ""}] and "details" not in c3.store.data  # no remember box, nothing kept


def test_in_your_own_window_ari_only_fills_fields():
    b = FakeBrowser(SITE, "https://shop.example/")
    c = ctx([{"do": "click", "n": 2}])
    try:
        br.run_task(c, b, "search", allow=br.FILL_ONLY)
    except AssertionError as e:  # the scripted model's click is refused by the check
        assert "do must be one of: type, select, done, ask" in str(e)
    else:
        raise AssertionError("a click was allowed in your window")


# ---------------------------------------------------------------- apps through UI Automation, and your own form

CALC = {"window: Calculator": {"title": "Calculator", "text": "Display is 0", "items": [
    {"n": 1, "role": "button", "name": "Seven"}, {"n": 2, "role": "button", "name": "Plus"},
    {"n": 3, "role": "button", "name": "Equals"}]}}
FORM = {"window: Visa application - Google Chrome": {"title": "Visa application - Google Chrome", "items": [
    {"n": 1, "role": "textbox", "name": "Full name"}, {"n": 2, "role": "textbox", "name": "Passport number"},
    {"n": 3, "role": "combobox", "name": "Purpose of visit"}, {"n": 4, "role": "button", "name": "Submit"}]}}


def test_a_task_in_an_app_on_the_workstation():
    d = FakeDesk([(1, "Word", "winword", "you"), (8, "Calculator", "calculatorapp", "ari")])
    surfaces = []

    def surface(hwnd):
        surfaces.append(hwnd)
        return FakeBrowser(CALC, "window: Calculator")

    c = plug_ctx(d, None, {"app": "calculator", "goal": "press seven"}, surface_for=surface,
                 decisions=[{"do": "click", "n": 1}, {"do": "done", "answer": "Pressed 7."}])
    out = ws.in_app(c)
    assert out["done"] and out["answer"] == "Pressed 7." and surfaces == [8] and d.fg == 1
    assert ws.in_app(plug_ctx(d, None, {"app": "DirectFN", "goal": "buy"}))["problem"] == "I don't work in DirectFN"


def test_fill_this_form_types_but_never_presses_submit():
    d = FakeDesk([(3, "Visa application - Google Chrome", "chrome", "you")])
    d.fg = 3
    b = FakeBrowser(FORM, "window: Visa application - Google Chrome")
    c = plug_ctx(d, None, {}, surface_for=lambda h: b,
                 decisions=[{"do": "type", "n": 1, "text": "Sasanka W"},
                            {"do": "select", "n": 3, "text": "Tourism"},
                            {"do": "done", "answer": "Filled your name and the purpose."}])
    out = ws.fill(c)
    assert out["done"] and out["filled"] == 2 and "press the button yourself" in out["answer"]
    assert [x["do"] for x in b.did] == ["type", "select"] and d.on[3] == "you"
    assert c.asked[0]["you_may"] == ["type", "select", "done", "ask"]
    b2 = FakeBrowser(FORM, "window: Visa application - Google Chrome")  # the passport number is yours to type
    out = ws.fill(plug_ctx(d, None, {}, surface_for=lambda h: b2,
                           decisions=[{"do": "type", "n": 2, "text": "N1234567"}]))
    assert out["handover"] and b2.did == []


def test_fill_this_form_said_plainly():
    assert straight_to({"fill_form": {}}, "Hey Ari, fill this form for me") == ("fill_form", {})
    assert straight_to({"fill_form": {}}, "fill in the form please") == ("fill_form", {})
