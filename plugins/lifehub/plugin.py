"""Life Hub: the shopping list, the wishlist and this month's money, through Life Hub's API (header X-API-Key).

Money is read only here, and private: what Cashly says never goes to Claude (the tool is marked private, so Ari's
thinking stays on the local models). Marking something bought and logging the expense stays in Life Hub, where you
see the new balance first.
"""

from __future__ import annotations

import json
from typing import Any

from argus.worker import Context, PermanentError, workflow

PLUGIN = "lifehub"
LISTS = ("shopping", "wishlist")


def call(ctx: Context, method: str, path: str, body: Any = None) -> Any:
    base = str(ctx.config.get("url") or "http://127.0.0.1:8081").rstrip("/")
    key = ctx.secrets.get("LIFEHUB_API_KEY")
    if not key:
        raise PermanentError("put Life Hub's API_KEY in Argus's .env as LIFEHUB_API_KEY")
    try:
        status, raw = ctx.http.request(method, base + path, json_body=body,
                                       headers={"X-API-Key": key, "Accept": "application/json"})
    except OSError:
        raise PermanentError(f"Life Hub isn't answering at {base} (docker compose up -d in its folder)") from None
    if status in (401, 403):
        raise PermanentError("Life Hub refused the key: LIFEHUB_API_KEY must equal API_KEY in lifehub/.env")
    if status >= 400:
        try:
            detail = json.loads(raw).get("detail")
        except (ValueError, AttributeError):
            detail = None
        raise PermanentError(f"Life Hub: {detail or f'HTTP {status}'}")
    return json.loads(raw) if raw else None


def which(ctx: Context) -> str:
    lst = str(ctx.input.get("list") or "shopping").strip().lower()
    if lst not in LISTS:
        raise PermanentError("the list is shopping or wishlist")
    return lst


def item(i: dict) -> dict:
    out = {k: i.get(k) for k in ("name", "quantity", "store", "price", "currency", "status") if i.get(k)}
    if i.get("insight"):
        out["affordable"] = i["insight"]
    return out


@workflow(PLUGIN, "lists")
def lists(ctx: Context):
    lst = which(ctx)

    def go():
        rows = call(ctx, "GET", f"/api/items?list={lst}") or []
        rows = [r for r in rows if r.get("status") != "bought"]
        return {"list": lst, "items": [item(r) for r in rows[:40]], "more": max(0, len(rows) - 40)}

    return ctx.step("list", go)


@workflow(PLUGIN, "add")
def add(ctx: Context):
    lst = which(ctx)
    text = " ".join(str(ctx.input.get("text") or "").split())
    if not text:
        raise PermanentError("add what?")

    def go():
        if ctx.dry_run:
            return {"would_add": text, "list": lst, "dry_run": True}
        return {"added": item(call(ctx, "POST", "/api/items/quick", {"list": lst, "text": text}) or {}), "list": lst}

    return ctx.step("add", go)


@workflow(PLUGIN, "money")
def money(ctx: Context):
    def go():
        m = call(ctx, "GET", "/api/money") or {}
        if not m.get("connected"):
            return {"connected": False, "why": m.get("error") or "Cashly isn't connected to Life Hub"}
        month = m.get("month") or {}
        return {"currency": m.get("home_currency"), "safe_to_spend": m.get("spare"),
                "until_days": m.get("spare_horizon_days"), "bills_before_payday": m.get("bills_before_payday"),
                "spent_this_month": month.get("expense"), "budget": month.get("budget"),
                "income_this_month": month.get("income"), "day": month.get("day"),
                "days_in_month": month.get("days_in_month"), "payday": m.get("payday")}

    return ctx.step("money", go)
