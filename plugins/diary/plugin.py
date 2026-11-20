"""Diary: Ari keeps a diary of your days in Life Hub.

You tell Ari how your day was ("my day was good, I fixed the login bug and went to the gym with Kasun"), in one go
in the evening or bit by bit. Every time, what you said is added to that day's record word for word, and the day's
entry is written again from all of it: a title, a first-person entry in your voice, highlights, the mood, people and
places. The writer only uses what you said (numbers in it are checked in code). Your words stay with the entry.

Where it lives: Life Hub (`PUT /api/diary/<date>`, shown on its Diary page with a calendar), not Obsidian; with
`vault_copy` a copy also goes to the notes vault (Diary/<year>/<date>.md). Things you say you learned go to Life
Hub's "learn" list, from where the notes vault files them.

Who writes it: `model: claude` (default) or `local` (the models on your machines; nothing leaves them). Claude
being away or over its daily budget falls back to the local models. You can also type into Life Hub's diary box
(or quick add "diary: ..."): `tidy` takes those every 10 minutes.

`reminders` (daily 04:00, or on demand) reads the last 14 days and finds what falls on today, for the morning
brief. It only ever uses the local models: the diary is private, so Claude is never asked.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from argus.models import EscalationExhausted
from argus.worker import Context, PermanentError, workflow

PLUGIN = "diary"
MOODS = ("great", "good", "okay", "low", "rough")
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


# ------------------------------------------------------------------ Life Hub

def call(ctx: Context, method: str, path: str, body: Any = None, missing_ok: bool = False) -> Any:
    base = str(ctx.config.get("lifehub_url") or "http://127.0.0.1:8081").rstrip("/")
    key = ctx.secrets.get("LIFEHUB_API_KEY")
    if not key:
        raise PermanentError("put Life Hub's API_KEY in Argus's .env as LIFEHUB_API_KEY")
    try:
        status, raw = ctx.http.request(method, base + path, json_body=body,
                                       headers={"X-API-Key": key, "Accept": "application/json"})
    except OSError:
        raise PermanentError(f"Life Hub isn't answering at {base}") from None
    if status == 404 and missing_ok:
        return None
    if status in (401, 403):
        raise PermanentError("Life Hub refused the key: LIFEHUB_API_KEY must equal API_KEY in lifehub/.env")
    if status == 404:
        raise PermanentError("this Life Hub has no diary yet: update it (docker compose up -d --build)")
    if status >= 400:
        try:
            detail = json.loads(raw).get("detail")
        except (ValueError, AttributeError):
            detail = None
        raise PermanentError(f"Life Hub: {detail or f'HTTP {status}'}")
    return json.loads(raw) if raw else None


# ------------------------------------------------------------------ which day

def which_day(said: str, now: dt.datetime, starts: int = 4) -> dt.date:
    """'today' (before `starts` o'clock: still yesterday), 'yesterday', a weekday (the last one), or YYYY-MM-DD."""
    today = (now - dt.timedelta(hours=starts)).date() if now.hour < starts else now.date()
    s = (said or "today").strip().lower()
    if s in ("", "today", "tonight", "this evening"):
        return today
    if s == "yesterday":
        return today - dt.timedelta(days=1)
    if s.removeprefix("last ") in WEEKDAYS:
        back = (today.weekday() - WEEKDAYS.index(s.removeprefix("last "))) % 7 or 7
        return today - dt.timedelta(days=back)
    try:
        d = dt.date.fromisoformat(s[:10])
    except ValueError:
        raise PermanentError(f"which day is {said!r}? (today, yesterday, a weekday or YYYY-MM-DD)") from None
    if d > today:
        raise PermanentError("that day hasn't happened yet")
    return d


# ------------------------------------------------------------------ writing the entry

class Entry(BaseModel):
    title: str = ""
    entry: str = ""
    highlights: list[str] = Field(default_factory=list)
    mood: Literal["great", "good", "okay", "low", "rough"] = "okay"
    people: list[str] = Field(default_factory=list)
    places: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    learned: list[str] = Field(default_factory=list)


WRITE = """You keep the user's diary. You get everything they said about one day (in order, with the time they said
it). Write that day's diary entry, in the first person, in their voice, as they would write it themselves: plain,
warm, not flowery.
- Use only what they said. Never add events, feelings, people, places, times or numbers they didn't say. If they
  said little, the entry is short.
- "title": a short title for the day (3-8 words, no date).
- "entry": the entry, 1-4 short paragraphs (markdown, no headings).
- "highlights": up to 5 short points (the day's main moments).
- "mood": one of great, good, okay, low, rough: how the day felt, from what they said.
- "people" and "places": the ones they mentioned, as they said them. "tags": up to 4 short lowercase topics.
- "learned": things they said they learned or found out (each a short sentence, as a note to keep), else empty.
- "your_edited_entry", when there: the user changed this day's entry themselves. Keep their title and their text
  word for word, and only add what's new in what they said since (a short paragraph at the end); keep their mood
  unless the new part clearly changes it.
Answer in JSON only."""

NUM = re.compile(r"\d+(?:[.,:]\d+)?")
STAMP = re.compile(r"^\d{1,2}:\d{2}\s+", re.M)  # the time each thing was said, at the start of its line


def times(text: str) -> str:
    """One way to write a time, so "7 .30", "7.30" and "7:30" are the same number."""
    return re.sub(r"(\d+) ?[.:](\d+)", r"\1:\2", text)


def entry_check(said: str, edited: dict | None = None):
    heard = said.lower()
    numbers = set(NUM.findall(times(STAMP.sub("", said))))
    # "19,20 December" is a list (19 and 20), but "4,495" is one number: a comma part of 3 digits is thousands
    numbers |= {p for n in list(numbers) if "," in n and len(n.split(",")[1]) != 3 for p in n.split(",")}
    kept = " ".join(str((edited or {}).get("entry") or "").split())

    def check(e: Entry, _inp: Any) -> str | None:
        if not e.entry.strip() or not e.title.strip():
            return "the entry and the title can't be empty"
        if kept and kept[:200] not in " ".join(e.entry.split()):
            return "keep the user's own edited text word for word (from \"your_edited_entry\"); only add after it"
        if len(e.entry) > 4000:
            return "the entry is too long: at most 4 short paragraphs"
        extra = [n for n in NUM.findall(times(e.entry + " " + e.title)) if n not in numbers]
        if extra:
            return f"these numbers weren't said: {', '.join(extra[:5])}. Use only what the user said"
        for who in [*e.people, *e.places]:
            ws = [w for w in re.findall(r"[a-z0-9']+", who.lower()) if len(w) > 2]
            if ws and not any(w in heard for w in ws):
                return f"{who!r} wasn't mentioned: list only people and places the user named"
        return None

    return check


def write(ctx: Context, day: dt.date, said: list[dict], edited: dict | None = None) -> Entry:
    """The entry from everything said about `day`, by the chosen writer (Claude by default; local on its own or
    when Claude can't). `edited`: the entry as you changed it in Life Hub: your text is kept and only added to."""
    text = "\n".join(f"{s.get('at', '')}  {s.get('text', '')}" for s in said)
    task: dict[str, Any] = {"day": day.isoformat(), "weekday": WEEKDAYS[day.weekday()].title(), "said": text[-12000:]}
    if edited:
        task["your_edited_entry"] = {"title": edited.get("title") or "", "entry": edited.get("entry") or ""}
    check = entry_check(text + ("\n" + str(edited.get("title") or "") + "\n" + str(edited.get("entry") or "")
                                if edited else ""), edited)
    if (ctx.config.get("model") or "claude") == "claude":
        try:
            return ctx.claude(WRITE, task, schema=Entry, check=check)
        except Exception as e:  # noqa: BLE001 - away, over budget, or the answer failed the check: local next
            ctx.log.info("diary: Claude didn't write it; the local models will", extra={"why": str(e)[:200]})
    try:
        return ctx.llm(WRITE, task, schema=Entry, check=check, tiers=ctx.local_tiers(), claude_last=False)
    except EscalationExhausted:  # still something: your words, as said
        if edited:  # your edited entry, and the new thing said under it
            return Entry(title=str(edited.get("title") or "My day"), mood=edited.get("mood") or "okay",
                         entry=f"{str(edited.get('entry') or '').rstrip()}\n\n{said[-1]['text']}".strip())
        first = said[0]["text"] if said else ""
        return Entry(title=" ".join(first.split()[:6]) or "My day",
                     entry="\n\n".join(s["text"] for s in said), mood="okay")


def clean(items: list[str], n: int, length: int = 80) -> list[str]:
    out = []
    for x in items:
        x = " ".join(str(x).split())[:length]
        if x and x.lower() not in {o.lower() for o in out}:
            out.append(x)
    return out[:n]


def as_markdown(day: dt.date, e: dict) -> str:
    said = "\n".join(f"> **{s.get('at', '')}** {s.get('text', '')}" for s in e.get("said") or [])
    hl = "\n".join(f"- {h}" for h in e.get("highlights") or [])
    meta = {"date": day.isoformat(), "mood": e.get("mood"), "people": e.get("people") or [],
            "places": e.get("places") or [], "tags": ["diary", *(e.get("tags") or [])]}
    fm = "---\n" + "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in meta.items()) + "\n---\n"
    return (f"{fm}\n# {e.get('title') or day.isoformat()}\n\n{e.get('entry', '').strip()}\n\n"
            + (f"## Highlights\n\n{hl}\n\n" if hl else "")
            + (f"> [!quote]- What I said\n{said}\n" if said else ""))


# ------------------------------------------------------------------ workflows

def tell_one(ctx: Context, text: str, day: dt.date, at: str, sfx: str = "") -> dict:
    old = ctx.step(f"load{sfx}", call, ctx, "GET", f"/api/diary/{day.isoformat()}", missing_ok=True) or {}
    said = [*(old.get("said") or []), {"at": at, "text": text}]
    return write_day(ctx, day, said, old, sfx)


def write_day(ctx: Context, day: dt.date, said: list[dict], old: dict, sfx: str = "", fresh: bool = False) -> dict:
    """Write `day`'s entry from everything said and save it (Life Hub, learned notes, the vault copy). `fresh`: a
    rewrite you asked for: the entry is Ari's again (Life Hub drops its "edited by you" mark)."""
    edited = old if old.get("edited_at") else None  # you changed it in Life Hub: your text stays
    e = ctx.step(f"write{sfx}", lambda: write(ctx, day, said, edited).model_dump())
    if edited:  # your title, and what you listed, stay too
        e["title"] = edited.get("title") or e["title"]
        for k in ("highlights", "people", "places", "tags"):
            e[k] = [*(edited.get(k) or []), *e[k]]
    entry = {"title": " ".join(e["title"].split())[:80], "entry": e["entry"].strip(),
             "highlights": clean(e["highlights"], 5), "mood": e["mood"] if e["mood"] in MOODS else "okay",
             "people": clean(e["people"], 10, 40), "places": clean(e["places"], 10, 40),
             "tags": clean([t.lower() for t in e["tags"]], 4, 24), "said": said,
             "model": ctx.last_answer.tier if getattr(ctx, "last_answer", None) else None}
    if fresh:
        entry["edited"] = False

    def save():
        if ctx.dry_run:
            return {"dry_run": True}
        call(ctx, "PUT", f"/api/diary/{day.isoformat()}", entry)
        return {"saved": day.isoformat()}

    out = ctx.step(f"save{sfx}", save)

    def learned():
        sent = set(ctx.store.get("learned_sent") or [])
        new = [x for x in clean(e.get("learned") or [], 5, 300) if f"{day}:{x.lower()}" not in sent]
        if ctx.dry_run or not new:
            return new
        for x in new:  # the notes vault takes Life Hub's "learn" list every 10 minutes and files each
            call(ctx, "POST", "/api/items", {"list": "learn", "name": x[:200], "notes": x, "store": "diary"})
            sent.add(f"{day}:{x.lower()}")
        ctx.store.set("learned_sent", sorted(sent)[-2000:])
        return new

    sent = ctx.step(f"learned{sfx}", learned)

    def copy():
        if not ctx.config.get("vault_copy"):
            return None
        root = Path(os.path.expanduser("~/Vault"))
        if not root.is_dir():  # no vault on this machine
            return None
        path = root / "Diary" / str(day.year) / f"{day.isoformat()}.md"
        text = as_markdown(day, entry)
        if ctx.files.exists(path):
            ctx.files.replace_text(path, text)
        else:
            ctx.files.write_text(path, text)
        return path.as_posix()

    ctx.step(f"vault copy{sfx}", copy)
    return {**out, "day": day.isoformat(), "title": entry["title"], "mood": entry["mood"], "learned": sent}


@workflow(PLUGIN, "tell")
def tell(ctx: Context):
    text = " ".join(str(ctx.input.get("text") or "").split())
    if not text:
        raise PermanentError("tell me about your day first")
    now = dt.datetime.now()
    day = which_day(str(ctx.input.get("day") or "today"), now, int(ctx.config.get("day_starts") or 4))
    out = tell_one(ctx, text, day, now.strftime("%H:%M"))
    when = "today's" if day == which_day("today", now, int(ctx.config.get("day_starts") or 4)) else \
        f"{WEEKDAYS[day.weekday()].title()}'s"
    out["say"] = (f"Added to {when} diary." if not ctx.dry_run else
                  "Dry-run: I'd add that to your diary. Turn the Diary plugin Live to keep it.") + \
        (f" I'll also put what you learned in your notes: {'; '.join(out['learned'][:2])}." if out["learned"] else "")
    return out


@workflow(PLUGIN, "rewrite")
def rewrite(ctx: Context):
    """"Rewrite this day": the entry written again from everything said that day, nothing added (for a day saved as
    you typed it, when no writer could do it then). A day you edited in Life Hub is rewritten too: your edit counts
    as something you said, and the entry is Ari's again."""
    starts = int(ctx.config.get("day_starts") or 4)
    day = which_day(str(ctx.input.get("day") or "today"), dt.datetime.now(), starts)
    old = ctx.step("load", call, ctx, "GET", f"/api/diary/{day.isoformat()}", missing_ok=True) or {}
    said = old.get("said") or []
    if not said:
        raise PermanentError(f"There's no diary entry for {day:%A %d %B} to rewrite.")
    if old.get("edited_at") and str(old.get("entry") or "").strip():
        # you fixed it by hand: your text counts as something you said (kept, so it isn't lost), and the whole
        # day is written again from all of it
        said = [*said, {"at": "edited", "text": " ".join(str(old["entry"]).split())[:8000]}]
        old = {**old, "edited_at": None}
    out = write_day(ctx, day, said, old, fresh=True)
    out["say"] = (f"Rewrote your diary for {day:%A %d %B}: {out['title']}." if not ctx.dry_run else
                  "Dry-run: I'd rewrite that day. Turn the Diary plugin Live to keep it.")
    return out


@workflow(PLUGIN, "read")
def read(ctx: Context):
    day = which_day(str(ctx.input.get("day") or "today"), dt.datetime.now(), int(ctx.config.get("day_starts") or 4))
    e = ctx.step("load", call, ctx, "GET", f"/api/diary/{day.isoformat()}", missing_ok=True)
    if not e:
        return {"day": day.isoformat(), "found": False, "say": f"There's no diary entry for {day:%A %d %B}."}
    return {"day": day.isoformat(), "found": True, "title": e.get("title"), "mood": e.get("mood"),
            "entry": e.get("entry"), "highlights": e.get("highlights") or []}


@workflow(PLUGIN, "tidy")
def tidy(ctx: Context):
    """What you typed into Life Hub's diary box (its "diary" list): into that day's entry, then off the list."""
    rows = ctx.step("life hub", call, ctx, "GET", "/api/items?list=diary", missing_ok=True) or []
    rows = [r for r in rows if not r.get("deleted") and (r.get("notes") or r.get("name"))]
    done = []
    for i, r in enumerate(sorted(rows, key=lambda r: str(r.get("created_at") or "")), 1):
        try:
            at = dt.datetime.fromisoformat(str(r.get("created_at")).replace("Z", "+00:00")).astimezone()
        except ValueError:
            at = dt.datetime.now().astimezone()
        starts = int(ctx.config.get("day_starts") or 4)
        day = which_day(str(r.get("target_date") or "today"), at.replace(tzinfo=None), starts)
        out = tell_one(ctx, " ".join(str(r.get("notes") or r.get("name")).split()), day, at.strftime("%H:%M"), f" {i}")
        if not ctx.dry_run:
            ctx.step(f"taken {i}", call, ctx, "DELETE", f"/api/items/{r['id']}")
        done.append(out)
    return {"taken": len(done), "days": sorted({d["day"] for d in done})}



# ------------------------------------------------------------------ reminders for today (for the morning brief)

DAYS_BACK = 14
TIME = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")


class Reminder(BaseModel):
    text: str = ""
    time: str | None = None
    mentioned_on: str = ""


class Reminders(BaseModel):
    reminders: list[Reminder] = Field(default_factory=list)


REMIND = """You read the last two weeks of the user's private diary and find what falls on TODAY (the date and weekday
are in "today"). You get each day's entry ("entry") and, word for word, what they said ("said").
Pick out only things that belong to today:
- appointments, meetings, bookings, deadlines and plans set for today ("dentist on Thursday" on a Thursday, "tomorrow
  at 3" said yesterday, "on the 20th" when today is the 20th);
- things they said they would do today or "this morning/afternoon/evening" of today;
- birthdays and anniversaries today.
Rules:
- Never invent anything. Every item must come from something they wrote or said, and numbers must be as they said.
- Skip anything already done, in the past, or for another day.
- "text": the item as a short note to the user (under 80 characters, no date). "time": 24-hour "HH:MM" only if a time
  was given for it, else null. "mentioned_on": the diary date (YYYY-MM-DD) where it was said.
- At most 8 items, most important first. If nothing falls on today, return an empty list.
Answer in JSON only."""


def reminders_check(sources: str, dates: set[str]):
    numbers = set(NUM.findall(times(sources)))

    def check(r: Reminders, _inp: Any) -> str | None:
        if len(r.reminders) > 8:
            return "at most 8 reminders"
        for x in r.reminders:
            if not x.text.strip() or len(x.text) > 160:
                return "every reminder needs short text (under 80 characters)"
            if x.time is not None and not TIME.match(x.time.strip()):
                return f"time {x.time!r} must be 24-hour HH:MM, or null when no time was given"
            if x.mentioned_on not in dates:
                return f"mentioned_on {x.mentioned_on!r} must be one of the diary dates you were given"
            extra = [n for n in NUM.findall(times(x.text)) if n not in numbers]
            if extra:
                return f"these numbers weren't in the diary: {', '.join(extra[:5])}. Use only what it says"
        return None

    return check


def diary_days(ctx: Context, today: dt.date) -> list[dict]:
    """The last DAYS_BACK days of entries (today's too), oldest first; days without an entry are left out."""
    out = []
    for back in range(DAYS_BACK, -1, -1):
        day = today - dt.timedelta(days=back)
        e = call(ctx, "GET", f"/api/diary/{day.isoformat()}", missing_ok=True)
        if e:
            said = " ".join(str(s.get("text") or "") for s in e.get("said") or [])
            out.append({"date": day.isoformat(), "weekday": WEEKDAYS[day.weekday()].title(),
                        "entry": str(e.get("entry") or "")[:1500], "said": said[:1500]})
    return out


@workflow(PLUGIN, "reminders")
def reminders(ctx: Context):
    """What the last 14 days of your diary say is today (appointments, things you said you would do, birthdays),
    for the morning brief. Runs at 04:00. The local models only, never Claude: the diary is private. The answer is
    this job's result (`{"for": "<today>", "reminders": [...]}`); the brief reads the newest one for today."""
    today = dt.date.fromisoformat(str(ctx.input.get("day"))) if ctx.input.get("day") else dt.date.today()
    days = ctx.step("load", diary_days, ctx, today)
    base = {"for": today.isoformat(), "days_read": len(days)}
    if not days:
        return {**base, "reminders": []}
    local = ctx.local_tiers()
    if not local:
        raise PermanentError("no local models are set up (models.tiers): the diary is never sent to Claude")
    task = {"today": {"date": today.isoformat(), "weekday": WEEKDAYS[today.weekday()].title()}, "diary": days}
    sources = "\n".join(f"{d['entry']} {d['said']}" for d in days) + f" {today.day}"
    dates = {d["date"] for d in days}

    def extract() -> dict:
        try:
            ans = ctx.llm(REMIND, task, schema=Reminders, check=reminders_check(sources, dates), tiers=local,
                          claude_last=False)
        except EscalationExhausted as e:
            return {"reminders": [], "problem": str(e)[:200]}
        return ans.model_dump()

    got = ctx.step("extract", extract)
    seen: set[tuple[str, str]] = set()
    items = []
    for x in got["reminders"]:
        text = " ".join(str(x.get("text") or "").split())[:80]
        key = (text.lower(), str(x.get("time") or ""))
        if text and key not in seen:
            seen.add(key)
            items.append({"text": text, "time": (x.get("time") or "").strip() or None,
                          "mentioned_on": x.get("mentioned_on") or None})
    return {**base, "reminders": items[:8], **({"problem": got["problem"]} if got.get("problem") else {})}
