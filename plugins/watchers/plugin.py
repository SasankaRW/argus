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

_DROP = re.compile(r"<(script|style|noscript|svg|template|head)\b.*?</\1\s*>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")
_BLOCK = re.compile(r"</?(p|div|li|tr|h[1-6]|br|section|article|td|th|ul|ol|table)\b[^>]*>", re.I)


def page_text(markup: str) -> str:
    """The words a person sees, one block per line."""
    t = _DROP.sub(" ", markup)
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
_NUM = r"\d{1,3}(?:[,\s]\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?"
PRICE = re.compile(rf"(?P<cur>{_CUR})\s?(?P<n>{_NUM})|(?P<n2>{_NUM})\s?(?P<cur2>{_CUR})(?![a-z])", re.I)


def _num(s: str) -> float | None:
    try:
        return float(re.sub(r"[,\s]", "", s))
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


def text_price(text: str) -> tuple[float, str] | None:
    for m in PRICE.finditer(text):
        n = _num(m.group("n") or m.group("n2"))
        if n:
            return n, (m.group("cur") or m.group("cur2") or "").strip()
    return None


def fmt(price: float, cur: str) -> str:
    n = f"{price:,.2f}".rstrip("0").rstrip(".") if price % 1 else f"{price:,.0f}"
    c = cur.strip().upper()
    return f"{c} {n}" if c and c not in ("$", "€", "£", "₹") else f"{cur.strip()}{n}"


def read(markup: str, w: dict[str, Any]) -> dict[str, Any]:
    """What a watch sees on the page now: {text, hash, price?, found}."""
    text = narrow(page_text(markup), w.get("part") or "")
    if text is None:
        return {"found": False}
    out: dict[str, Any] = {"found": True, "text": text[:4000],
                           "hash": hashlib.sha256(text.encode()).hexdigest()[:16]}
    if w["kind"] == "price":
        p = (None if w.get("part") else structured_price(markup)) or text_price(text)
        if p:
            out["price"], out["cur"] = p
    return out


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
        if p is None:
            return None
        cur = now.get("cur") or last.get("cur") or ""
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
    kind = str(ctx.input.get("kind") or ("price" if ctx.input.get("below") is not None else "change")).lower()
    if kind not in ("change", "price"):
        raise PermanentError('kind is "change" or "price"')
    part = " ".join(str(ctx.input.get("part") or "").split())[:80]
    below = ctx.input.get("below")
    try:
        below = float(below) if below not in (None, "") else None
    except (TypeError, ValueError):
        raise PermanentError("the price to watch for should be a number") from None
    w = {"name": " ".join(str(ctx.input.get("name") or "").split())[:60] or name_for(url, part), "url": url,
         "kind": kind, "below": below, "part": part}

    def first_look():
        now = read(fetch(ctx, url), w)
        if not now["found"]:
            raise PermanentError(f"couldn't find \"{part}\" on that page")
        if kind == "price" and "price" not in now:
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
            say += f": it's {fmt(now['price'], now.get('cur', ''))} now"
            say += f"; I'll tell you when it's under {fmt(below, now.get('cur', ''))}." if below is not None \
                else "; I'll tell you when it drops."
        else:
            say += "; I'll tell you when it changes."
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
            all_ = watches(ctx)
            w = all_.get(name)
            if w is None:  # stopped meanwhile
                return None
            try:
                now = read(fetch(ctx, w["url"]), w)
                problem = None if now["found"] else f"\"{w['part']}\" isn't on the page any more"
            except Exception as e:  # one page down doesn't stop the others
                now, problem = None, str(e)[:200]
            msg = verdict(w, now) if now else None
            if now and now["found"]:
                w["last"] = now
                if msg:
                    w["changed_at"] = time.time()
            w["checked_at"], w["problem"] = time.time(), problem
            all_[name] = w
            ctx.store.set("watches", all_)
            if msg:
                ctx.notify("Watcher: " + w["name"], msg, priority="high", tags=["eyes"], link=w["url"])
            return {"told": msg, "problem": problem}

        r = ctx.step(f"check {name}", one)
        if r and r["told"]:
            told.append(r["told"])
        if r and r["problem"]:
            problems.append(f"{name}: {r['problem']}")
    return {"checked": len(todo), "told": told, "problems": problems}
