"""Advisor plugin: advice may only use the numbers Life Hub worked out; alternatives only links and prices the search
found."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("t_advisor", ROOT / "plugins" / "advisor" / "plugin.py")
ad = importlib.util.module_from_spec(spec)
sys.modules["t_advisor"] = ad  # so pydantic can see the module's names
spec.loader.exec_module(ad)

FACTS = {"position": {"free_cash": 265660, "emergency_months": 1.8, "card_debt": 42300},
         "payday_plan": [{"to": "Emergency fund", "amount": 42750}], "today": "2026-10-27"}


def test_advice_uses_only_given_numbers():
    check = ad.advice_check(FACTS)
    ok = ad.Advice(text="Pay the Rs 42,300 card first, then put Rs 42,750 into the emergency fund (1.8 months now).")
    assert check(ok, None) is None
    assert "not in FACTS" in check(ad.Advice(text="Save Rs 50,000 this month."), None)
    assert check(ad.Advice(text="Free cash is Rs 265,700."), None) is None  # within 1%: rounding is fine
    assert check(ad.Advice(text="Aim for 6 months of cover over 3 steps."), None) is None  # small counts are fine
    assert "empty" in check(ad.Advice(text=" "), None)


def test_the_question_counts_as_given():
    check = ad.advice_check(FACTS, "can I afford a 95,000 phone?")
    assert check(ad.Advice(text="A Rs 95,000 phone would leave Rs 170,660 free."), None) is not None
    assert check(ad.Advice(text="Not yet: the Rs 95,000 phone should wait until the card is paid."), None) is None


def test_alternatives_only_from_search_results():
    found = [{"title": "Sony WH-1000XM4 - Rs 72,500 | Daraz", "url": "https://daraz.lk/xm4", "snippet": "brand new"},
             {"title": "Used XM5", "url": "https://ikman.lk/xm5", "snippet": "Rs. 68,000 negotiable"}]
    check = ad.alternatives_check(found)
    good = ad.Alternatives(summary="The XM4 is much cheaper.", options=[
        ad.Option(name="Sony WH-1000XM4", kind="older model", price="Rs 72,500", link="https://daraz.lk/xm4"),
        ad.Option(name="Used XM5", kind="used", price="Rs. 68,000", link="https://ikman.lk/xm5")])
    assert check(good, None) is None
    made_up_link = good.model_copy(update={"options": [ad.Option(name="x", link="https://example.com/x")]})
    assert "not one of the search results" in check(made_up_link, None)
    made_up_price = good.model_copy(update={"options": [ad.Option(name="x", price="Rs 55,000", link="https://daraz.lk/xm4")]})
    assert "isn't in the search results" in check(made_up_price, None)


def test_watchers_prices_are_read():
    assert ad.shown_price("Rs 95,000") == (95000.0, "LKR")
    assert ad.shown_price("$1,299.99") == (1299.99, "USD")
    assert ad.shown_price("LKR 42,500") == (42500.0, "LKR")
    assert ad.shown_price("not a price") is None


def test_rates_only_from_search_results():
    found = [{"title": "FD rates October", "url": "https://x.lk/fd", "snippet": "12 month FD 8.50% p.a."},
             {"title": "T-bill auction", "url": "https://cbsl.lk/tb", "snippet": "364-day yield 9.12 per cent"}]
    check = ad.rates_check(found)
    assert check(ad.Rates(fd_12m=8.5, tbill_364=9.12, sources=["https://x.lk/fd"]), None) is None
    assert "isn't written" in check(ad.Rates(fd_12m=7.25), None)
    assert "sources" in check(ad.Rates(fd_12m=8.5, sources=["https://made.up"]), None)
    assert "no rate" in check(ad.Rates(), None)


class FakeLifeHubCtx:
    """Just enough of a worker Context for the prices workflow: Life Hub over http, Watchers over tools."""

    def __init__(self, links, watches, old_lifehub=False):
        self.config, self.dry_run, self.input = {}, False, {}
        self.secrets = {"LIFEHUB_API_KEY": "k"}
        self.links, self.watches, self.old = links, watches, old_lifehub
        self.posted, self.tools, self.saved = [], [], {}
        self.store = self
        self.http = self

    # store
    def get(self, key, default=None):
        return self.saved.get(key, default)

    def set(self, key, value):
        self.saved[key] = value

    # http (Life Hub)
    def request(self, method, url, json_body=None, headers=None):
        import json as j
        path = url.split("8081", 1)[1]
        if path == "/api/advisor/links":
            return (404, b'{"detail": "Not Found"}') if self.old else (200, j.dumps(self.links).encode())
        if path.startswith("/api/items"):
            return 200, j.dumps([{"uid": k["uid"], "name": k["name"], "url": k["url"], "status": "idea"}
                                 for k in self.links if k["main"]]).encode()
        if path == "/api/advisor/prices":
            self.posted.append(json_body)
            return 200, b'{"ok": true}'
        raise AssertionError(path)

    def step(self, name, fn, *a, **kw):
        return fn(*a, **kw)

    def tool(self, name, inp):
        self.tools.append((name, inp))
        if name == "list_watches":
            return {"watches": list(self.watches.values())}
        if name == "watch_page":
            if "nothing.lk" in inp["url"]:
                raise ad.ToolFailed("couldn't find a price on that page")
            self.watches[inp["url"]] = {"name": inp["name"], "url": inp["url"], "kind": "price",
                                        "price": "LKR 3,800", "stock": "in"}
            return {"watching": inp["name"]}
        if name == "stop_watching":
            self.watches = {u: w for u, w in self.watches.items() if w["name"] != inp["name"]}
            return {"stopped": inp["name"]}
        raise AssertionError(name)


DUNE = "https://www.jeyabookcentre.com/item/64632-dune-1"


def test_every_shop_link_is_watched_with_its_stock():
    links = [{"uid": "u1", "name": "Dune book", "url": DUNE, "main": True, "shop": "jeyabookcentre.com"},
             {"uid": "u1", "name": "Dune book", "url": "https://sarasavi.lk/dune", "main": False,
              "shop": "sarasavi.lk"},
             {"uid": "u1", "name": "Dune book", "url": "https://nothing.lk/x", "main": False, "shop": "nothing.lk"}]
    watches = {DUNE: {"name": "Wishlist: Dune book", "url": DUNE, "kind": "price", "price": "LKR 3,996",
                      "stock": "out"},
               "https://old.lk/gone": {"name": "Wishlist: Old thing", "url": "https://old.lk/gone", "kind": "price"},
               "https://mine.lk": {"name": "RTX on mine.lk", "url": "https://mine.lk", "kind": "price"}}
    ctx = FakeLifeHubCtx(links, watches)
    out = ad.prices(ctx)
    assert out["watching_new"] == ["Dune book · sarasavi.lk"]  # its own name, so it doesn't replace the main one
    assert out["stopped"] == ["Wishlist: Old thing"]  # a link that's gone; your own watches stay
    assert "https://mine.lk" in ctx.watches
    assert list(ctx.saved["unwatchable_at"]) == ["https://nothing.lk/x"]
    assert ctx.posted == [
        {"uid": "u1", "currency": "LKR", "price": 3996.0, "in_stock": False, "url": DUNE,
         "source": "watch:jeyabookcentre.com"},
        {"uid": "u1", "currency": "LKR", "price": 3800.0, "in_stock": True, "url": "https://sarasavi.lk/dune",
         "source": "watch:sarasavi.lk"}]
    again = ad.prices(ctx)
    assert again["watching_new"] == [] and again["no_price_found"] == []  # a page without a price: not every run
    ctx.saved["unwatchable_at"] = {"https://nothing.lk/x": 0}
    assert ad.prices(ctx)["no_price_found"]  # but again the next day


def test_a_misread_price_never_reaches_life_hub():
    links = [{"uid": "u1", "name": "Dune book", "url": DUNE, "main": True, "item_price": 4000}]
    watches = {DUNE: {"name": "Wishlist: Dune book", "url": DUNE, "kind": "price",
                      "price": "LKR 9,780,441,172,719", "stock": "out"}}  # what the old reader saw
    ctx = FakeLifeHubCtx(links, watches)
    ad.prices(ctx)
    assert ctx.posted == [{"uid": "u1", "currency": None, "price": None, "in_stock": False, "url": DUNE,
                           "source": "watch:jeyabookcentre.com"}]  # only the stock


def test_an_older_life_hub_without_links_still_works():
    links = [{"uid": "u1", "name": "Dune book", "url": DUNE, "main": True}]
    ctx = FakeLifeHubCtx(links, {}, old_lifehub=True)
    out = ad.prices(ctx)
    assert out["watching_new"] == ["Dune book"] and ctx.posted[0]["source"] == "watch:jeyabookcentre.com"
    assert ad.watch_name({"name": "x" * 80, "main": False, "url": "https://sarasavi.lk/a"}).endswith(" · sarasavi.lk")
    assert len(ad.watch_name({"name": "x" * 80, "main": False, "url": "https://sarasavi.lk/a"})) <= 60
