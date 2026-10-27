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
