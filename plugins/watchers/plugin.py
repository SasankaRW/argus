"""Watchers: web pages checked every 3 hours; the phone hears when one changes or a price drops.

A watch is {name, url, kind: change|price, below, part}. "part" narrows it to the text after some words on the
page (a product name), so a clock or an ad elsewhere on the page doesn't count as a change. Prices come from the
page's own product data (JSON-LD, og:price) when it has it, else the first price in the watched text.

Only public websites (permissions.network "*": never this machine, the LAN or the tailnet). Nothing is bought:
it only reads pages and tells you.
"""

from __future__ import annotations

import difflib
import hashlib
import html
import json
import re
import time
import urllib.parse
from typing import Any

from argus.worker import Context, PermanentError, workflow

PLUGIN = "watchers"
MAX_PAGE = 2_000_000
PART_LEN = 800
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) argus-watchers/1.0",
      "Accept-Language": "en;q=0.9"}

# ------------------------------------------------------------------ reading a page

_OPEN = re.compile(r"<(script|style|noscript|svg|template|head)\b", re.I)


def _drop_hidden(markup: str) -> str:
    """Remove script, style and the like, in one pass (a regex with .*? goes quadratic on unclosed tags)."""
    out, i, low = [], 0, markup.lower()
    while True:
        m = _OPEN.search(markup, i)
        if not m:
            out.append(markup[i:])
            return " ".join(out)
        out.append(markup[i:m.start()])
        end = low.find(f"</{m.group(1).lower()}", m.end())
        if end < 0:
            return " ".join(out)  # never closed: the rest is hidden
        close = low.find(">", end)
        i = len(markup) if close < 0 else close + 1
_TAG = re.compile(r"<[^>]+>")
_BLOCK = re.compile(r"</?(p|div|li|tr|h[1-6]|br|section|article|td|th|ul|ol|table)\b[^>]*>", re.I)


def page_text(markup: str) -> str:
    """The words a person sees, one block per line."""
    t = _drop_hidden(markup)
    t = _BLOCK.sub("\n", t)
    t = html.unescape(_TAG.sub(" ", t))
    lines = (" ".join(x.split()) for x in t.splitlines())
    return "\n".join(x for x in lines if x)


def narrow(text: str, part: str) -> str | None:
    """The text from the first place `part` appears (case and spacing ignored), PART_LEN long; None if absent."""
    if not part:
        return text
    words = [re.escape(w) for w in part.split()]
    m = re.search(r"\s+".join(words), text, re.I)
    return text[m.start():m.start() + PART_LEN] if m else None


_CUR = r"(?:rs\.?|lkr|usd|us\$|\$|€|eur|£|gbp|₹|inr)"
_NUM = r"\d{1,3}(?:[,  ]\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?"
# Number and currency on the same line only: "EAN13 9780441172719" above "LKR 3,996.00" is not a price.
PRICE = re.compile(rf"(?P<cur>{_CUR})[  ]?(?P<n>{_NUM})|(?P<n2>{_NUM})[  ]?(?P<cur2>{_CUR})(?![a-z])",
                   re.I)
LONG_CODE = re.compile(r"\d{10,}")  # an ISBN, barcode or phone number, never a price


def _num(s: str) -> float | None:
    try:
        return float(re.sub(r"[,\s ]", "", s))
    except ValueError:
        return None


def structured_price(markup: str) -> tuple[float, str] | None:
    """The page's own product price: JSON-LD offers, or og:/product:price:amount meta tags."""
    for block in re.findall(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", markup, re.S | re.I):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        stack = [data]
        while stack:
            d = stack.pop()
            if isinstance(d, list):
                stack.extend(d)
            elif isinstance(d, dict):
                offers = d.get("offers")
                for o in offers if isinstance(offers, list) else [offers] if offers else []:
                    if isinstance(o, dict):
                        p = o.get("price") or o.get("lowPrice")
                        if p is not None and _num(str(p)) is not None:
                            return _num(str(p)), str(o.get("priceCurrency") or "")  # type: ignore[return-value]
                stack.extend(v for v in d.values() if isinstance(v, (dict, list)))
    m = re.search(r'<meta[^>]+(?:property|name)=["\'](?:og|product):price:amount["\'][^>]*content=["\']([\d.,]+)',
                  markup, re.I)
    if m and _num(m.group(1)) is not None:
        cur = re.search(r'<meta[^>]+(?:property|name)=["\'](?:og|product):price:currency["\'][^>]*content=["\']'
                        r'([A-Z]{3})', markup, re.I)
        return _num(m.group(1)), cur.group(1) if cur else ""  # type: ignore[return-value]
    return None


def _text_price_match(text: str) -> tuple[float, str, re.Match] | None:
    for m in PRICE.finditer(text):
        raw = m.group("n") or m.group("n2")
        if LONG_CODE.fullmatch(raw.split(".")[0]):
            continue
        n = _num(raw)
        if n:
            return n, (m.group("cur") or m.group("cur2") or "").strip(), m
    return None


def text_price(text: str) -> tuple[float, str] | None:
    found = _text_price_match(text)
    return (found[0], found[1]) if found else None


# ------------------------------------------------------------------ in stock or not

_LD_STOCK = {"instock": "in", "instoreonly": "in", "onlineonly": "in", "limitedavailability": "in",
             "preorder": "in", "presale": "in", "backorder": "in",
             "outofstock": "out", "soldout": "out", "discontinued": "out"}
_OUT_WORDS = re.compile(r"out of stock|sold out|currently unavailable|unavailable|not available|no stock|"
                        r"temporarily out|notify me when", re.I)
_IN_WORDS = re.compile(r"in stock|add to (?:cart|basket|bag)|buy now|available now|ready to ship", re.I)
MISREAD = 100  # a price this many times higher or lower than the last is a misread, not a sale
STOCK_AFTER = 200  # characters after the price that still describe that product
STOCK_BEFORE = 60


def structured_stock(markup: str) -> str | None:
    """'in' or 'out' from the page's own product data (JSON-LD offers.availability, og:availability)."""
    for block in re.findall(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", markup, re.S | re.I):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        stack = [data]
        while stack:
            d = stack.pop()
            if isinstance(d, list):
                stack.extend(d)
            elif isinstance(d, dict):
                offers = d.get("offers")
                for o in offers if isinstance(offers, list) else [offers] if offers else []:
                    if isinstance(o, dict) and o.get("availability"):
                        key = str(o["availability"]).rsplit("/", 1)[-1].lower()
                        if key in _LD_STOCK:
                            return _LD_STOCK[key]
                stack.extend(v for v in d.values() if isinstance(v, (dict, list)))
    m = re.search(r'<meta[^>]+(?:property|name)=["\'](?:og|product):availability["\'][^>]*content=["\']([^"\']+)',
                  markup, re.I)
    if m:
        v = m.group(1).lower().replace(" ", "").replace("_", "")
        if v in ("instock", "available", "preorder", "pending"):
            return "in"
        if v in ("oos", "outofstock", "soldout", "discontinued"):
            return "out"
    return None


def text_stock(text: str, near: re.Match | None, narrowed: bool = False) -> str | None:
    """'in' or 'out' from the words right around the price (a whole page also lists other products).
    "Out of stock" wins over an "Add to cart" button that's still on the page. Without a price, only a watch
    narrowed to its product ("part") is read, from the product's name on."""
    if near is None:
        if not narrowed:
            return None
        window = text[:STOCK_BEFORE + STOCK_AFTER]
    else:
        window = text[max(0, near.start() - STOCK_BEFORE):near.end() + STOCK_AFTER]
    if _OUT_WORDS.search(window):
        return "out"
    if _IN_WORDS.search(window):
        return "in"
    return None


UNDER = re.compile(rf"(?:(?:tell me )?(?:when )?(?:it'?s |it is )?(?:under|below|less than|<)\s*)(?:{_CUR}\s?)?"
                   rf"(?P<n>{_NUM})(?:\s?{_CUR})?", re.I)


def note_price(note: str) -> tuple[float | None, str]:
    """A shared link's note: "under 250,000" (or a price with its currency) is the price to watch for; other
    words are the part of the page to watch. -> (price or None, the other words)."""
    m = UNDER.search(note) or PRICE.search(note)
    if not m:
        return None, note.strip()
    n = _num(m.group("n") or m.groupdict().get("n2") or "")
    return n, (note[:m.start()] + note[m.end():]).strip(" ,.-:")


def fmt(price: float, cur: str) -> str:
    n = f"{price:,.2f}".rstrip("0").rstrip(".") if price % 1 else f"{price:,.0f}"
    c = cur.strip().rstrip(".").upper()
    c = {"RS": "Rs", "USD": "$", "US$": "$", "EUR": "€", "GBP": "£", "INR": "₹"}.get(c, c)
    return f"{c}{n}" if c in ("$", "€", "£", "₹") else f"{c} {n}" if c else n


def read(markup: str, w: dict[str, Any]) -> dict[str, Any]:
    """What a watch sees on the page now: {text, hash, price?, cur?, stock?, found}. stock is "in" or "out"."""
    text = narrow(page_text(markup), w.get("part") or "")
    if text is None:
        return {"found": False}
    out: dict[str, Any] = {"found": True, "text": text[:4000],
                           "hash": hashlib.sha256(text.encode()).hexdigest()[:16]}
    if w["kind"] == "price":
        whole_page = not w.get("part")
        found = _text_price_match(text)
        p = (structured_price(markup) if whole_page else None) or (found[:2] if found else None)
        if p:
            out["price"], out["cur"] = p
        near = found[2] if found else None
        stock = (structured_stock(markup) if whole_page else None) or text_stock(text, near, narrowed=not whole_page)
        if stock:
            out["stock"] = stock
    return out


def stock_words(stock: str | None) -> str:
    return {"in": "in stock", "out": "out of stock"}.get(stock or "", "")


def what_changed(old: str, new: str) -> str:
    """The first lines that are new, in a sentence or two."""
    added = [ln for ln in difflib.ndiff(old.splitlines(), new.splitlines()) if ln.startswith("+ ")]
    s = "; ".join(a[2:].strip() for a in added[:3])
    return s[:280] + ("…" if len(s) > 280 else "")


def verdict(w: dict[str, Any], now: dict[str, Any]) -> str | None:
    """The phone message for this check, or None when there is nothing to tell."""
    last = w.get("last") or {}
    if not now.get("found"):
        return None
    if w["kind"] == "price":
        p, before = now.get("price"), last.get("price")
        cur = now.get("cur") or last.get("cur") or ""
        stock, was = now.get("stock"), last.get("stock")
        at = f" at {fmt(p, cur)}" if p is not None else ""
        if stock == "in" and was == "out":
            return f"{w['name']} is back in stock{at}."
        if stock == "out" and was == "in":
            return f"{w['name']} is out of stock now."
        if p is None:
            return None
        if before and not before / MISREAD < p < before * MISREAD:
            return None  # a hundredfold jump is a misread (or the old one was): start again from this price
        below = w.get("below")
        if below is not None and p <= below and (before is None or before > below):
            return f"{w['name']} is {fmt(p, cur)}, under your {fmt(below, cur)}."
        if before is not None and p < before and below is None:
            return f"{w['name']} dropped to {fmt(p, cur)} (was {fmt(before, cur)})."
        return None
    if last.get("hash") and now["hash"] != last["hash"]:
        change = what_changed(last.get("text", ""), now["text"])
        return f"{w['name']} changed" + (f": {change}" if change else ".")
    return None


# ------------------------------------------------------------------ the workflows

def fetch(ctx: Context, url: str) -> str:
    status, body = ctx.http.request("GET", url, headers=UA, max_bytes=MAX_PAGE)
    if status >= 400:
        raise PermanentError(f"the page answered HTTP {status}")
    return body.decode("utf-8", errors="replace")


def watches(ctx: Context) -> dict[str, dict[str, Any]]:
    return ctx.store.get("watches") or {}


def name_for(url: str, part: str) -> str:
    host = (urllib.parse.urlparse(url).hostname or url).removeprefix("www.")
    return f"{part} on {host}" if part else host


@workflow(PLUGIN, "add")
def add(ctx: Context):
    url = str(ctx.input.get("url") or "").strip()
    if url and "://" not in url:
        url = "https://" + url
    if not re.fullmatch(r"https?://[^\s/$.?#][^\s]{2,500}", url):
        raise PermanentError("that doesn't look like a web address")
    below = ctx.input.get("below")
    part = " ".join(str(ctx.input.get("part") or "").split())[:80]
    note = " ".join(str(ctx.input.get("note") or "").split())  # shared from the phone: "under 250,000" or words
    if note:
        price, rest = note_price(note)
        if below in (None, ""):
            below = price
        part = part or rest[:80]
    kind = str(ctx.input.get("kind") or ("price" if below not in (None, "") else "change")).lower()
    if kind not in ("change", "price"):
        raise PermanentError('kind is "change" or "price"')
    try:
        below = float(below) if below not in (None, "") else None
    except (TypeError, ValueError):
        raise PermanentError("the price to watch for should be a number") from None
    given = ctx.input.get("name") or ctx.input.get("title")  # a shared link's page title
    w = {"name": " ".join(str(given or "").split())[:60] or name_for(url, part), "url": url,
         "kind": kind, "below": below, "part": part}

    def first_look():
        now = read(fetch(ctx, url), w)
        if not now["found"]:
            raise PermanentError(f"couldn't find \"{part}\" on that page")
        if kind == "price" and "price" not in now and "stock" not in now:  # stock alone: back-in-stock news
            raise PermanentError("couldn't find a price on that page (name the product with \"part\")")
        return now

    now = ctx.step("first look", first_look)

    def keep():
        all_ = watches(ctx)
        if len(all_) >= int(ctx.config.get("max_watches", 25)) and w["name"] not in all_:
            raise PermanentError("that's as many watches as I keep; stop one first")
        all_[w["name"]] = {**w, "last": now, "checked_at": time.time(), "changed_at": None, "added_at": time.time()}
        ctx.store.set("watches", all_)
        say = f"Watching {w['name']}"
        if kind == "price":
            if "price" in now:
                say += f": it's {fmt(now['price'], now.get('cur', ''))} now"
                if now.get("stock"):
                    say += f" ({stock_words(now['stock'])})"
            else:
                say += f": no price shown, {stock_words(now['stock'])}"
            say += f"; I'll tell you when it's under {fmt(below, now.get('cur', ''))}." if below is not None \
                else "; I'll tell you when it drops."
        else:
            say += "; I'll tell you when it changes."
        if kind == "price" and now.get("stock") == "out":
            say += " I'll also tell you when it's back in stock."
        return {"watching": w["name"], "say": say}

    return ctx.step("keep", keep)


@workflow(PLUGIN, "list")
def list_(ctx: Context):
    def go():
        out = []
        for w in watches(ctx).values():
            last = w.get("last") or {}
            row = {"name": w["name"], "url": w["url"], "kind": w["kind"]}
            if w["kind"] == "price" and "price" in last:
                row["price"] = fmt(last["price"], last.get("cur", ""))
                if w.get("below") is not None:
                    row["tell_under"] = fmt(w["below"], last.get("cur", ""))
            if w["kind"] == "price" and last.get("stock"):
                row["stock"] = last["stock"]
            row["changed"] = time.strftime("%d %b %H:%M", time.localtime(w["changed_at"])) if w.get("changed_at") \
                else "not since added"
            if w.get("problem"):
                row["problem"] = w["problem"]
            out.append(row)
        return {"watches": out}

    return ctx.step("list", go)


@workflow(PLUGIN, "remove")
def remove(ctx: Context):
    want = " ".join(str(ctx.input.get("name") or "").split()).lower()
    if not want:
        raise PermanentError("which watch?")

    def go():
        all_ = watches(ctx)
        hits = [k for k, w in all_.items() if want in (k.lower(), w["url"].lower())] or \
            [k for k, w in all_.items() if want in k.lower() or want in w["url"].lower()]
        if not hits:
            raise PermanentError(f"no watch called {want!r}")
        if len(hits) > 1:
            raise PermanentError("more than one fits: " + ", ".join(hits[:5]))
        del all_[hits[0]]
        ctx.store.set("watches", all_)
        return {"stopped": hits[0]}

    return ctx.step("remove", go)


@workflow(PLUGIN, "check")
def check(ctx: Context):
    todo = sorted(watches(ctx))
    told, problems = [], []
    for name in todo:
        def one(name=name):
            w = watches(ctx).get(name)
            if w is None:  # stopped meanwhile
                return None
            try:
                now = read(fetch(ctx, w["url"]), w)
                problem = None if now["found"] else f"\"{w['part']}\" isn't on the page any more"
            except Exception as e:  # one page down doesn't stop the others
                now, problem = None, str(e)[:200]
            msg = verdict(w, now) if now else None
            if now and now["found"]:
                before = w.get("last") or {}
                if w["kind"] == "price" and "price" not in now and "price" in before:
                    now = {**now, "price": before["price"], "cur": before.get("cur", "")}  # a read without a price
                if w["kind"] == "price" and "stock" not in now and "stock" in before:
                    now = {**now, "stock": before["stock"]}  # a read that didn't say: keep what we knew
                w["last"] = now
                if msg:
                    w["changed_at"] = time.time()
            w["checked_at"], w["problem"] = time.time(), problem
            fresh = watches(ctx)  # read again: a watch added or stopped while the page loaded stays that way
            if name in fresh:
                fresh[name] = w
                ctx.store.set("watches", fresh)
            if msg:
                ctx.notify("Watcher: " + w["name"], msg, priority="high", tags=["eyes"], link=w["url"])
            return {"told": msg, "problem": problem}

        r = ctx.step(f"check {name}", one)
        if r and r["told"]:
            told.append(r["told"])
        if r and r["problem"]:
            problems.append(f"{name}: {r['problem']}")
    return {"checked": len(todo), "told": told, "problems": problems}
