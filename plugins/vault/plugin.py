"""Notes vault: your Obsidian vault (~/Vault), kept in order for you.

Everything you want to keep lands in `00 Inbox` first: a note you write in Obsidian (phone, PC, laptop; Syncthing
keeps the copies in step over Tailscale), "Ari, add to my notes on Docker: ...", the share menu (a link, text or a
picture) or Life Hub's quick add ("learn: ..."). Then:

- `capture` (Ari, your diary, the share menu): what you said is written up as a clean note first (title, gist,
  bullets; your words folded under it) by the chosen writer (`model`: claude or local), unless `write_spoken` is off.
- `file` (the Inbox watch, 5 minutes after a note stops changing): finds the closest notes (local embeddings),
  and the chosen model picks the place: added to an existing topic note as a dated section, or a new topic note
  under an area (`Topics/<Area>/<Title>.md`). The note's text is copied word for word. Links go both ways in each
  note's "Related" section, new topics are listed on their area's map (`Maps/<Area> map.md`). With `filing: auto`
  (the default) you are asked only when it isn't sure or for a new area; with `ask`, every time. The capture is
  moved to `00 Inbox/_done`.
- `tidy` (every 10 minutes): Life Hub's "learn:" notes into the Inbox, notes you marked for enhancing, Syncthing
  conflict copies (you hear about each once), and anything in the Inbox the watch missed.
- `enhance`: a note marked `#enhance` (or `enhance: true`, or "Ari, enhance my note on ...") is rewritten by
  Claude with web search: summary, key ideas, explanation, examples, mistakes, questions, sources. Your own text
  stays in the note, folded under "My original notes". Unmarked notes never leave this machine.

Existing notes are only changed through ctx.files.replace_text: never one you (or a sync) touched in the last 2
minutes, and the old version is kept in ~/.argus-trash/versions first.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import math
import os
import re
import time
import urllib.parse
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from argus.models import EscalationExhausted
from argus.worker import Context, PermanentError, WaitSignal, workflow

PLUGIN = "vault"
INBOX, DONE = "00 Inbox", "00 Inbox/_done"
TOPICS, MAPS, SOURCES, ATTACH = "Topics", "Maps", "Sources", "Attachments"
RECENT = 120  # seconds: a note changed this recently is being edited (or just synced); leave it for now
LEFTOVER = 600  # Inbox notes older than this that nobody is filing are picked up by tidy
SURE = 0.6  # T1's confidence below this: ask where it goes (filing: auto)
CLOSE = 0.35  # similarity for "related" (embeddings)
CLOSE_WORDS = 0.15  # the same, from shared words when there are no embeddings
UA = {"User-Agent": "Mozilla/5.0 (Argus notes vault)", "Accept": "text/html,application/xhtml+xml"}
BAD_NAME = re.compile(r'[\\/:*?"<>|#^\[\]]+')


def vault() -> Path:
    return Path(os.path.expanduser("~/Vault"))


def today() -> str:
    return dt.date.today().isoformat()


# ------------------------------------------------------------------ frontmatter and sections

FM = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)


def split_fm(text: str) -> tuple[dict[str, Any], str]:
    """(frontmatter, the rest). Text without frontmatter (or with broken YAML) gives ({}, text)."""
    m = FM.match(text)
    if not m:
        return {}, text
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return {}, text
    if not isinstance(meta, dict):
        return {}, text
    return {str(k): (v.isoformat() if isinstance(v, (dt.date, dt.datetime)) else v) for k, v in meta.items()}, \
        text[m.end():].lstrip("\r\n")


def _scalar(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return ""
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s) or re.fullmatch(r"[A-Za-z][\w .()/+-]*", s) and \
            s.lower() not in ("true", "false", "yes", "no", "null", "on", "off"):
        return s
    return json.dumps(s, ensure_ascii=False)


def dump_fm(meta: dict[str, Any]) -> str:
    lines = ["---"]
    for k, v in meta.items():
        if isinstance(v, (list, tuple)):
            lines.append(f"{k}: [{', '.join(_scalar(x) for x in v)}]")
        else:
            lines.append(f"{k}: {_scalar(v)}".rstrip())
    return "\n".join(lines) + "\n---\n"


RELATED = re.compile(r"(?m)^## Related[ \t]*$")
LINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")


def related_span(body: str) -> tuple[int, int] | None:
    """Where the "## Related" section is (start of its heading, end of the section)."""
    m = RELATED.search(body)
    if not m:
        return None
    nxt = re.compile(r"(?m)^#{1,2} ").search(body, m.end())
    return m.start(), nxt.start() if nxt else len(body)


def add_related(body: str, titles: list[str]) -> str:
    """Body with [[title]] added to its Related section (made at the end if missing); existing links kept."""
    span = related_span(body)
    have = set()
    if span:
        have = {x.strip().lower() for x in LINK.findall(body[span[0]:span[1]])}
    new = [t for t in titles if t.lower() not in have]
    if not new:
        return body
    lines = "".join(f"- [[{t}]]\n" for t in new)
    if span is None:
        return body.rstrip() + "\n\n## Related\n\n" + lines
    s, e = span
    section = body[s:e].rstrip() + "\n" + lines
    return body[:s] + section + ("\n" + body[e:] if e < len(body) else "")


def add_section(body: str, heading: str, text: str) -> str:
    """A new "## heading" section with `text` (as given), just before Related (or at the end)."""
    block = f"## {heading}\n\n{text.strip()}\n\n"
    span = related_span(body)
    if span is None:
        return body.rstrip() + "\n\n" + block
    return body[:span[0]].rstrip() + "\n\n" + block + body[span[0]:]


def clean_title(t: str, limit: int = 80) -> str:
    t = BAD_NAME.sub(" ", t or "")
    t = re.sub(r"\s+", " ", t).strip(" .-")
    return t[:limit].strip(" .-")


def first_line(text: str) -> str:
    for ln in text.splitlines():
        ln = re.sub(r"^[#>*\-\s]+", "", ln).strip()
        if ln:
            return ln
    return ""


def slug(text: str, n: int = 50) -> str:
    return clean_title(first_line(text) or "note", n) or "note"


# ------------------------------------------------------------------ the vault's notes

def topic_notes(ctx: Context) -> list[dict]:
    """Topic notes: Topics/<Area>/<Title>.md."""
    root = vault() / TOPICS
    if not ctx.files.is_dir(root):
        return []
    out = []
    for path, _size, mtime in ctx.files.walk(root, limit=20_000):
        p = Path(path)
        if p.suffix.lower() != ".md" or p.parent == root:
            continue
        out.append({"path": path, "rel": p.relative_to(vault()).as_posix(), "title": p.stem,
                    "area": p.relative_to(root).parts[0], "mtime": mtime})
    return out


def areas(ctx: Context) -> list[str]:
    root = vault() / TOPICS
    if not ctx.files.is_dir(root):
        return []
    return sorted(Path(p).name for p in ctx.files.list(root) if ctx.files.is_dir(p))


def note_text(ctx: Context, n: dict, limit: int = 1500) -> str:
    try:
        _meta, body = split_fm(ctx.files.read_text(n["path"], limit=limit + 2000))
    except OSError:
        return ""
    return body[:limit]


WORD = re.compile(r"[a-z0-9]{3,}")


def words(text: str) -> set[str]:
    return set(WORD.findall(text.lower()))


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def vectors(ctx: Context, notes: list[dict]) -> dict[str, list[float]] | None:
    """Embedding per note (cached by modified time in this plugin's data folder); None without embeddings."""
    cache_file = Path(ctx.data_dir) / "vectors.json" if ctx.data_dir else None
    cache: dict[str, list] = {}
    if cache_file and cache_file.exists():
        try:
            cache = json.loads(cache_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cache = {}
    todo = [n for n in notes if not (n["rel"] in cache and abs(cache[n["rel"]][0] - n["mtime"]) < 0.001)]
    for i in range(0, len(todo), 16):
        batch = todo[i:i + 16]
        got = ctx.embed([f"{n['title']} ({n['area']})\n{note_text(ctx, n)}" for n in batch])
        if got is None:
            return None
        for n, v in zip(batch, got, strict=False):
            cache[n["rel"]] = [n["mtime"], v]
    keep = {n["rel"] for n in notes}
    cache = {k: v for k, v in cache.items() if k in keep}
    if cache_file:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(cache), encoding="utf-8")
    return {k: v[1] for k, v in cache.items()}


def closest(ctx: Context, text: str, notes: list[dict], k: int = 6) -> list[dict]:
    """The k notes nearest to `text`, each with a score (cosine of embeddings, or shared words without them)."""
    if not notes:
        return []
    vecs = vectors(ctx, notes)
    q = ctx.embed([text[:4000]]) if vecs is not None else None
    scored = []
    if vecs is not None and q:
        for n in notes:
            if n["rel"] in vecs:
                scored.append({**n, "score": round(cosine(q[0], vecs[n["rel"]]), 3)})
    else:
        w = words(text)
        for n in notes:
            nw = words(f"{n['title']} {n['title']} {note_text(ctx, n, 800)}")
            score = len(w & nw) / math.sqrt(max(1, len(w)) * max(1, len(nw))) if w and nw else 0.0
            scored.append({**n, "score": round(score, 3), "words": True})
    scored.sort(key=lambda n: -n["score"])
    return scored[:k]


# ------------------------------------------------------------------ captures (what lands in the Inbox)

def write_capture(ctx: Context, body: str, meta: dict[str, Any]) -> str:
    stamp = time.strftime("%Y-%m-%d %H%M")
    name = f"{stamp} {slug(meta.get('title') or body)}.md"
    meta = {k: v for k, v in meta.items() if v not in (None, "", [], False)}
    text = (dump_fm(meta) if meta else "") + body.strip() + "\n"
    return ctx.files.write_text(vault() / INBOX / name, text)


def read_capture(ctx: Context, path: str) -> dict | None:
    if not ctx.files.exists(path):
        return None
    st = ctx.files.stat(path)
    meta, body = split_fm(ctx.files.read_text(path))
    title = clean_title(str(meta.get("title") or ""))
    if not title:
        m = re.search(r"(?m)^#\s+(.+)$", body)
        title = clean_title(m.group(1) if m else first_line(body), 60)
    return {"path": path, "name": Path(path).name, "meta": meta, "body": body.strip(), "title": title,
            "mtime": st.st_mtime}


def in_inbox(path: str) -> bool:
    p = Path(path)
    same = os.path.normcase(str(p.parent.resolve())) == os.path.normcase(str((vault() / INBOX).resolve()))
    return same and p.suffix.lower() == ".md" and not p.name.startswith(".")


# ------------------------------------------------------------------ choosing the place

class Place(BaseModel):
    action: Literal["add", "new"]
    note: str | None = None
    area: str = ""
    title: str = ""
    tags: list[str] = Field(default_factory=list)
    summary: str = ""
    confidence: float = 0.5


PLACE = """You file notes into the user's Obsidian vault of things they learn. You get a new note (its text and,
if the user gave one, a topic hint), the vault's areas and the closest existing topic notes. Decide where it goes:
- "add": it is about the same subject as one existing topic note: set "note" to that note's title, exactly as given.
- "new": a new topic: "area" is one of the existing areas if one fits, otherwise a short new area (1-3 words, Title
  Case, broad, e.g. "Machine Learning", "Docker", "Personal Finance"); "title" is the topic (1-6 words, Title Case,
  no dates, no area name in it).
Prefer adding to an existing note when it is clearly the same subject, and an existing area over a new one.
"tags": 1-3 short lowercase tags. "summary": one plain line (at most 100 characters) on what the note is about.
"confidence": 0 to 1, how sure you are of the place. Answer in JSON only."""


def place_check(titles: list[str], known: list[str]):
    def check(p: Place, _inp: Any) -> str | None:
        if p.action == "add":
            if not p.note or p.note.strip().lower() not in {t.lower() for t in titles}:
                return f"note must be one of the listed titles: {titles}"
            return None
        if not clean_title(p.title):
            return "a new note needs a title"
        if not clean_title(p.area, 40):
            return "a new note needs an area"
        if len(p.title) > 80 or len(p.area) > 40:
            return "title or area too long"
        return None
    return check


def ask_model(ctx: Context, prompt: str, task: Any, *, schema: Any, check: Any = None, subject: Any = None) -> Any:
    """The chosen writer (config `model`): Claude, falling back to the local models when Claude is away or over its
    daily budget; or the local models only (nothing leaves your machines)."""
    if (ctx.config.get("model") or "claude") == "claude":
        try:
            return ctx.claude(prompt, task, schema=schema, check=check)
        except Exception as e:  # noqa: BLE001
            ctx.log.info("vault: Claude didn't answer; the local models will", extra={"why": str(e)[:200]})
    return ctx.llm(prompt, task, schema=schema, check=check, tiers=ctx.local_tiers(), claude_last=False,
                   subject=subject)


class Written(BaseModel):
    title: str = ""
    note: str = ""
    topic: str = ""


WRITE_UP = """The user told their assistant something they learned or want to keep. Turn it into a short, clean
note for their Obsidian vault of things they learn.
- "title": the subject (1-6 words, Title Case, no dates).
- "note": the note in markdown: a one-line gist, then the points as short bullets. Use only what they said;
  fix the spoken wording (filler words, repeats, mis-heard words that are obvious), but add no new facts.
- "topic": the broader topic it belongs to (1-3 words), or the one they named.
Answer in JSON only."""


def write_up(ctx: Context, said: str, topic: str = "") -> dict | None:
    """What you said, as a clean note (title + body with your words folded under it). None: keep it as said."""
    def check(w: Written, _inp: Any) -> str | None:
        if not clean_title(w.title) or not w.note.strip():
            return "a title and a note are needed"
        if len(w.note) > max(1500, 3 * len(said)):
            return "too long: only what the user said, as short bullets"
        return None

    try:
        w = ask_model(ctx, WRITE_UP, {"said": said[:6000], "topic_hint": topic or None}, schema=Written, check=check)
    except EscalationExhausted:
        return None
    quote = "\n".join("> " + ln if ln.strip() else ">" for ln in said.strip().splitlines())
    return {"title": clean_title(w.title), "topic": topic or clean_title(w.topic, 40),
            "body": f"{w.note.strip()}\n\n> [!quote]- What I said\n{quote}"}


def decide(ctx: Context, cap: dict) -> dict:
    """Where the capture goes (not written yet): {action, note, area, title, tags, summary, related, sure, why}."""
    notes = topic_notes(ctx)
    hint = str(cap["meta"].get("topic") or "")
    near = closest(ctx, f"{hint}\n{cap['title']}\n{cap['body']}", notes)
    known = areas(ctx)
    cands = [{"title": n["title"], "area": n["area"], "similarity": n["score"],
              "start": note_text(ctx, n, 300)} for n in near]
    task = {"note": cap["body"][:4000], "topic_hint": hint or None, "areas": known, "closest_notes": cands}
    try:
        p = ask_model(ctx, PLACE, task, schema=Place, check=place_check([n["title"] for n in near], known),
                      subject=cap["path"])
    except EscalationExhausted:
        p = Place(action="new", area=hint or (near[0]["area"] if near else "Inbox"), title=cap["title"] or "Note",
                  confidence=0.0)
    by_title = {n["title"].lower(): n for n in near}
    target = by_title.get((p.note or "").strip().lower()) if p.action == "add" else None
    area = clean_title(target["area"] if target else p.area, 40)
    title = target["title"] if target else clean_title(p.title) or cap["title"] or "Note"
    if not target:  # titles are unique across the vault, so [[Title]] links never point at two notes
        taken = {n["title"].lower(): n for n in notes}
        if title.lower() in taken:
            same = taken[title.lower()]
            if same["area"].lower() == area.lower():
                target = same
            else:
                title = f"{title} ({area})"
    related = [n["title"] for n in near if n["score"] >= (CLOSE_WORDS if n.get("words") else CLOSE)
               and (not target or n["rel"] != target["rel"])][:3]
    why = []
    if p.confidence < SURE:
        why.append("not sure")
    if not target and area.lower() not in {a.lower() for a in known} and area.lower() != hint.strip().lower():
        why.append(f"new area {area}")
    return {"action": "add" if target else "new", "note": target["rel"] if target else None, "area": area,
            "title": title, "tags": [re.sub(r"[^\w-]", "", t.lower())[:24] for t in p.tags[:3] if t.strip()],
            "summary": " ".join(p.summary.split())[:100], "related": related, "sure": not why,
            "why": ", ".join(why)}


def confirm(ctx: Context, cap: dict, plan: dict) -> dict | None:
    """Ask where it goes (filing: ask, or not sure). None: you said no (it stays in the Inbox)."""
    notes = {n["title"].lower(): n for n in topic_notes(ctx)}
    current = Path(plan["note"]).stem if plan["note"] else ""
    pending = ctx.store.get("pending") or {}
    pending[cap["name"]] = time.time()
    ctx.store.set("pending", pending)
    ans = ctx.ask_me(f"Where should \"{cap['title'][:60]}\" go?",
                     {"area": plan["area"], "title": plan["title"], "add_to": current},
                     summary=[cap["body"][:300], "Add to an existing note: put its title in add_to. "
                              "Empty add_to: a new note (area / title).",
                              *([f"Close notes: {', '.join(plan['related'])}"] if plan["related"] else []),
                              *([f"Why I ask: {plan['why']}"] if plan["why"] else [])])
    pending = ctx.store.get("pending") or {}
    pending.pop(cap["name"], None)
    ctx.store.set("pending", pending)
    if ans is None:
        left = ctx.store.get("left") or []
        ctx.store.set("left", sorted(set(left) | {cap["name"]})[-500:])
        return None
    add_to = str(ans.get("add_to") or "").strip().lower()
    if add_to and add_to in notes:
        n = notes[add_to]
        return {**plan, "action": "add", "note": n["rel"], "area": n["area"], "title": n["title"]}
    area = clean_title(str(ans.get("area") or plan["area"]), 40) or "Inbox"
    title = clean_title(str(ans.get("title") or plan["title"])) or cap["title"] or "Note"
    if title.lower() in notes and notes[title.lower()]["area"].lower() == area.lower():
        n = notes[title.lower()]
        return {**plan, "action": "add", "note": n["rel"], "area": n["area"], "title": n["title"]}
    if title.lower() in notes:
        title = f"{title} ({area})"
    return {**plan, "action": "new", "note": None, "area": area, "title": title}


# ------------------------------------------------------------------ writing

class Busy(Exception):  # noqa: N818
    """A note was changed a moment ago (you, or Syncthing). An ordinary error: the job tries again later."""


def fresh(ctx: Context, path: str) -> float:
    """The note's modified time, or Busy if it changed in the last RECENT seconds."""
    m = ctx.files.stat(path).st_mtime
    if time.time() - m < RECENT:
        raise Busy(f"{Path(path).name} was changed a moment ago; trying again later")
    return m


def capture_sources(cap: dict) -> list[str]:
    src = cap["meta"].get("source_url") or cap["meta"].get("url")
    return [str(src)] if src else []


def write_new(ctx: Context, cap: dict, plan: dict) -> str:
    meta = {"area": plan["area"], "tags": plan["tags"], "created": today(), "updated": today(),
            "sources": capture_sources(cap), "status": "seed", "enhance": bool(cap["meta"].get("enhance"))}
    body = f"# {plan['title']}\n\n{cap['body']}\n"
    if plan["related"]:
        body = add_related(body, plan["related"])
    path = vault() / TOPICS / plan["area"] / f"{plan['title']}.md"
    return ctx.files.write_text(path, dump_fm(meta) + "\n" + body.lstrip("\n"))


def write_add(ctx: Context, cap: dict, plan: dict) -> str:
    path = str(vault() / plan["note"])
    mtime = fresh(ctx, path)
    meta, body = split_fm(ctx.files.read_text(path))
    stamp = today()
    heading = stamp if not cap["title"] or cap["title"].lower() == plan["title"].lower() else f"{stamp} {cap['title']}"
    body = add_section(body, heading, cap["body"])
    if plan["related"]:
        body = add_related(body, plan["related"])
    meta["updated"] = stamp
    srcs = [s for s in (meta.get("sources") or []) if s] if isinstance(meta.get("sources"), list) else []
    meta["sources"] = srcs + [s for s in capture_sources(cap) if s not in srcs]
    if cap["meta"].get("enhance"):
        meta["enhance"] = True
    if meta.get("status") == "seed":
        meta["status"] = "growing"
    ctx.files.replace_text(path, dump_fm(meta) + "\n" + body.lstrip("\n"), expect_mtime=mtime)
    return path


def link_back(ctx: Context, title: str, related: list[str]) -> list[str]:
    """Add [[title]] to each related note's Related section (skipping notes being edited right now)."""
    done = []
    by_title = {n["title"]: n for n in topic_notes(ctx)}
    for r in related:
        n = by_title.get(r)
        if not n or n["title"] == title:
            continue
        try:
            mtime = fresh(ctx, n["path"])
        except Busy:
            continue
        text = ctx.files.read_text(n["path"])
        meta, body = split_fm(text)
        new = add_related(body, [title])
        if new != body:
            out = (dump_fm(meta) + "\n" + new.lstrip("\n")) if meta else new
            ctx.files.replace_text(n["path"], out, expect_mtime=mtime)
            done.append(r)
    return done


def add_to_map(ctx: Context, area: str, title: str, summary: str) -> None:
    path = vault() / MAPS / f"{area} map.md"
    if not ctx.files.exists(path):
        ctx.files.write_text(path, f"# {area}\n\nEverything in {area}, newest last.\n\n")
        home = vault() / "Home.md"
        if ctx.files.exists(home):
            ctx.files.append_text(home, f"- [[{area} map|{area}]]\n")
    ctx.files.append_text(path, f"- [[{title}]]" + (f" – {summary}" if summary else "") + "\n")


def file_one(ctx: Context, path: str, n: int = 0) -> dict:
    """File one Inbox note. Step names carry `n`, so tidy can file several in one job."""
    sfx = f" {n}" if n else ""
    cap = ctx.step(f"read{sfx}", read_capture, ctx, path)
    if cap is None:
        return {"skipped": Path(path).name, "why": "already gone"}
    if not cap["body"]:
        return {"skipped": cap["name"], "why": "empty"}
    plan = ctx.step(f"place{sfx}", decide, ctx, cap)

    def ask():
        if (ctx.config.get("filing") or "auto") == "auto" and plan["sure"]:
            return plan
        return confirm(ctx, cap, plan)

    plan = ctx.step(f"confirm{sfx}", ask)
    if plan is None:
        return {"left": cap["name"], "why": "you said no; it stays in the Inbox"}

    def write():
        if not ctx.files.exists(cap["path"]):
            return {"skipped": cap["name"], "why": "already gone"}
        if plan["action"] == "add":
            where = write_add(ctx, cap, plan)
        else:
            where = write_new(ctx, cap, plan)
            add_to_map(ctx, plan["area"], plan["title"], plan["summary"])
        linked = link_back(ctx, plan["title"], plan["related"])
        ctx.files.move(cap["path"], vault() / DONE / cap["name"])
        return {"note": Path(where).relative_to(vault()).as_posix() if not ctx.dry_run else where,
                "action": plan["action"], "area": plan["area"], "title": plan["title"], "linked": linked,
                "dry_run": ctx.dry_run}

    out = ctx.step(f"write{sfx}", write)
    if "note" in out:
        ctx.saved(90, key=f"file{sfx}")
    return out


@workflow(PLUGIN, "file")
def file(ctx: Context):
    path = str(ctx.input.get("path") or "")
    if not path:
        raise PermanentError("no path in the job input")
    if not in_inbox(path):
        return {"skipped": path, "why": "not a note in the Inbox"}
    return file_one(ctx, path)


# ------------------------------------------------------------------ capture (Ari, the share menu, Life Hub)

def page_info(markup: str) -> dict:
    def meta(name: str) -> str:
        n = re.escape(name)
        m = re.search(rf'<meta[^>]+(?:property|name)=["\']{n}["\'][^>]*content=["\']([^"\']*)', markup, re.I) \
            or re.search(rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:property|name)=["\']{n}["\']', markup, re.I)
        return html.unescape(m.group(1)).strip() if m else ""

    t = re.search(r"(?is)<title[^>]*>(.*?)</title>", markup)
    title = meta("og:title") or (html.unescape(re.sub(r"\s+", " ", t.group(1))).strip() if t else "")
    body = re.sub(r"(?is)<(script|style|nav|footer|header|noscript)[^>]*>.*?</\1>", " ", markup)
    paras = [html.unescape(re.sub(r"<[^>]+>", " ", p)) for p in re.findall(r"(?is)<p[^>]*>(.*?)</p>", body)]
    paras = [" ".join(p.split()) for p in paras if len(p.split()) > 6]
    return {"title": title[:150], "description": (meta("og:description") or meta("description"))[:400],
            "text": "\n\n".join(paras)[:2500]}


class Picture(BaseModel):
    text: str = ""
    about: str = ""


SEE = """Read this picture for the user's notes. "text": all readable text in it, as written (empty if none).
"about": one line saying what it shows. Answer in JSON only."""


@workflow(PLUGIN, "capture")
def capture(ctx: Context):
    inp = ctx.input
    text = str(inp.get("text") or "").strip()
    note = str(inp.get("note") or "").strip()
    url = str(inp.get("url") or "").strip()
    files = inp.get("files") or []
    if not (text or url or files or note):
        raise PermanentError("nothing to save")
    meta: dict[str, Any] = {"source": str(inp.get("source") or ("share" if (url or files) else "ari")),
                            "topic": str(inp.get("topic") or "").strip(), "enhance": bool(inp.get("enhance"))}
    parts: list[str] = []

    if url:
        if "://" not in url:
            url = "https://" + url
        if not re.fullmatch(r"https?://\S{3,800}", url):
            raise PermanentError("that doesn't look like a web address")

        def source():
            info = {"title": str(inp.get("title") or ""), "description": "", "text": ""}
            try:
                status, raw = ctx.http.request("GET", url, headers=UA, max_bytes=2_000_000, public_only=True)
                if status < 400:
                    info = {**page_info(raw.decode("utf-8", errors="replace")),
                            **({"title": info["title"]} if info["title"] else {})}
            except (OSError, PermanentError) as e:  # offline, or not a public page: keep the link anyway
                ctx.log.warning("link not read", extra={"error": str(e)[:200]})
            title = clean_title(info["title"] or urllib.parse.urlparse(url).hostname or "Link", 90)
            body = f"# {title}\n\n{url}\n\n" + (f"> {info['description']}\n\n" if info["description"] else "") + \
                (info["text"] + "\n" if info["text"] else "")
            where = ctx.files.write_text(vault() / SOURCES / f"{title}.md",
                                         dump_fm({"url": url, "saved": today()}) + "\n" + body)
            return {"title": Path(where).stem, "description": info["description"]}

        src = ctx.step("source", source)
        meta["source_url"] = url
        parts.append(f"Source: [[{src['title']}]]" + (f" – {src['description']}" if src["description"] else ""))

    if files:
        if ctx.shared is None:
            raise PermanentError("no shared files available to this job")

        def pictures():
            out = []
            for f in files[:5]:
                name = clean_title(Path(str(f.get("name") or "picture.png")).stem, 60) or "picture"
                ext = Path(str(f.get("name") or "")).suffix.lower() or ".png"
                data = ctx.shared(f["name"])
                where = ctx.files.write_bytes(vault() / ATTACH / f"{time.strftime('%Y-%m-%d')} {name}{ext}", data)
                seen = None
                if ext in (".png", ".jpg", ".jpeg", ".webp"):
                    try:
                        seen = ctx.llm(SEE, {"name": name}, schema=Picture, images=[data], tiers=["V1"],
                                       claude_last=False)
                    except Exception as e:  # no vision model: the picture is kept without its text
                        ctx.log.warning("picture not read", extra={"error": str(e)[:200]})
                out.append({"file": Path(where).name, "text": seen.text.strip() if seen else "",
                            "about": seen.about.strip() if seen else ""})
            return out

        for p in ctx.step("pictures", pictures):
            parts.append(f"![[{p['file']}]]" + (f"\n\n{p['about']}" if p["about"] else "") +
                         ("\n\n> " + p["text"].replace("\n", "\n> ") if p["text"] else ""))

    spoken = meta["source"] in ("ari", "diary") and text and not url and not files
    if spoken and ctx.config.get("write_spoken", True):
        up = ctx.step("write up", write_up, ctx, text, meta["topic"])
        if up:
            text = up["body"]
            meta["title"] = up["title"]
            meta["topic"] = meta["topic"] or up["topic"]
    for t in (note, text):
        if t and t not in parts:
            parts.insert(0, t)
    if inp.get("title") and not url:
        meta["title"] = str(inp["title"])

    where = ctx.step("write", write_capture, ctx, "\n\n".join(parts), meta)
    ctx.saved(30, key="capture")
    say = ("Saved to your notes inbox; I'll file it in a minute or two." if not ctx.dry_run else
           "Dry-run: I'd save that to your notes inbox. Turn the Notes vault plugin Live to keep notes.")
    return {"saved": where, "say": say, "dry_run": ctx.dry_run}


# ------------------------------------------------------------------ enhance (Claude, only when you mark a note)

START, END = "<!-- enhanced:start -->", "<!-- enhanced:end -->"
QUOTE = re.compile(r"(?m)^> \[!quote\]- My original notes[ \t]*\n((?:>.*(?:\n|$))*)")
MARK = re.compile(r"(?<![\w/])#enhance\b")


def marked(meta: dict, body: str) -> bool:
    return meta.get("enhance") is True or bool(MARK.search(body))


def parts_of(body: str) -> dict:
    """A topic note's pieces: title line, the enhanced block, your original notes, the rest you wrote, Related."""
    title_m = re.match(r"\s*#\s+(.+)\n", body)
    title = title_m.group(1).strip() if title_m else ""
    rest = body[title_m.end():] if title_m else body
    span = related_span(rest)
    related = rest[span[0]:span[1]].strip() if span else ""
    if span:
        rest = rest[:span[0]] + rest[span[1]:]
    enhanced = ""
    s, e = rest.find(START), rest.find(END)
    if s != -1 and e > s:
        enhanced = rest[s + len(START):e].strip()
        rest = rest[:s] + rest[e + len(END):]
    originals = ""
    q = QUOTE.search(rest)
    if q:
        originals = "\n".join(re.sub(r"^> ?", "", ln) for ln in q.group(1).splitlines()).strip()
        rest = rest[:q.start()] + rest[q.end():]
    mine = MARK.sub("", rest).strip()
    return {"title": title, "enhanced": enhanced, "originals": originals, "mine": mine, "related": related}


ENHANCE = """You improve one note in the user's study vault (Obsidian, Markdown). Rewrite what they wrote into a
clear, correct note they can learn from. Check facts with web search and fix mistakes (say what you corrected).
Use these sections (leave out any that don't fit): a 2-3 line summary at the top (no heading), "## Key ideas",
"## Explanation", "## Examples" (code in fenced blocks when it helps), "## Common mistakes", "## Test yourself"
(3-5 questions), "## Sources" (links you used, as Markdown links). Keep the user's own points, examples and wording
where they are right. Be concise. You may link other notes with [[Title]], but only titles from `vault_titles`.
Do not write frontmatter, the note's title, a "Related" section or the user's original notes: those are kept
separately. Answer with the Markdown only."""


def enhance_check(text: Any, _inp: Any) -> str | None:
    t = str(text or "").strip()
    if len(t) < 200:
        return "too short: write the whole note"
    if t.startswith("---"):
        return "no frontmatter, only the note's Markdown"
    return None


def budget_left(ctx: Context) -> int:
    used = ctx.store.get("enhanced_today") or {}
    return max(0, int(ctx.config.get("enhance_per_day") or 10) - int(used.get(today(), 0)))


def enhance_one(ctx: Context, rel: str, n: int = 0) -> dict:
    sfx = f" {n}" if n else ""
    path = str(vault() / rel)

    def ask():
        if not ctx.files.exists(path):
            return None
        fresh(ctx, path)
        meta, body = split_fm(ctx.files.read_text(path))
        p = parts_of(body)
        originals = "\n\n".join(x for x in (p["originals"], p["mine"]) if x)
        titles = sorted({x["title"] for x in topic_notes(ctx)} - {Path(rel).stem})[:300]
        task = {"title": p["title"] or Path(rel).stem, "area": meta.get("area") or Path(rel).parent.name,
                "their_notes": originals[:12000], "previous_version": p["enhanced"][:8000] or None,
                "vault_titles": titles}
        text = str(ctx.claude(ENHANCE, task, check=enhance_check, web=True)).strip()
        known = {t.lower() for t in titles}
        text = LINK.sub(lambda m: m.group(0) if m.group(1).strip().lower() in known else m.group(1), text)
        return {"text": text}

    got = ctx.step(f"ask claude{sfx}", ask)
    if got is None:
        return {"skipped": rel, "why": "gone"}

    def write():
        mtime = fresh(ctx, path)
        meta, body = split_fm(ctx.files.read_text(path))
        p = parts_of(body)
        originals = "\n\n".join(x for x in (p["originals"], p["mine"]) if x)
        srcs = meta.get("sources") if isinstance(meta.get("sources"), list) else []
        found = re.findall(r"\]\((https?://[^)\s]+)\)", got["text"])
        meta.update(enhance=False, enhanced=today(), updated=today(),
                    sources=list(dict.fromkeys([*srcs, *found]))[:20])
        if meta.get("status") in (None, "seed"):
            meta["status"] = "growing"
        quoted = "\n".join(f"> {ln}" if ln else ">" for ln in originals.splitlines())
        out = f"# {p['title'] or Path(rel).stem}\n\n{START}\n{got['text']}\n{END}\n"
        if originals:
            out += f"\n> [!quote]- My original notes\n{quoted}\n"
        if p["related"]:
            out += f"\n{p['related']}\n"
        ctx.files.replace_text(path, dump_fm(meta) + "\n" + out, expect_mtime=mtime)
        used = ctx.store.get("enhanced_today") or {}
        ctx.store.set("enhanced_today", {today(): int(used.get(today(), 0)) + 1})
        return {"enhanced": rel, "sources": len(found), "dry_run": ctx.dry_run}

    out = ctx.step(f"write enhanced{sfx}", write)
    ctx.saved(600, key=f"enhance{sfx}")
    return out


def find_note(ctx: Context, topic: str) -> dict | None:
    notes = topic_notes(ctx)
    t = topic.strip().lower()
    exact = [n for n in notes if n["title"].lower() == t]
    if exact:
        return exact[0]
    near = closest(ctx, topic, notes, k=1)
    return near[0] if near and near[0]["score"] >= (CLOSE_WORDS if near[0].get("words") else CLOSE) else None


@workflow(PLUGIN, "enhance")
def enhance(ctx: Context):
    topic = str(ctx.input.get("topic") or ctx.input.get("note") or "").strip()
    if not topic:
        raise PermanentError("which note?")
    n = ctx.step("find", find_note, ctx, topic)
    if n is None:
        raise PermanentError(f"I couldn't find a note on {topic} in your vault")
    if budget_left(ctx) <= 0:
        raise PermanentError("today's enhancing is used up (Notes vault settings: enhance_per_day)")
    out = enhance_one(ctx, n["rel"])
    if "enhanced" in out:
        out["say"] = f"Done: your note on {n['title']} is enhanced. Your own text is kept under \"My original notes\"."
    return out


# ------------------------------------------------------------------ find

@workflow(PLUGIN, "find")
def find(ctx: Context):
    q = str(ctx.input.get("query") or "").strip()
    if not q:
        raise PermanentError("what should I look for?")

    def look():
        notes = topic_notes(ctx)
        out = []
        for n in closest(ctx, q, notes, k=5):
            if n["score"] < 0.15:
                continue
            text = note_text(ctx, n, 4000)
            ws = words(q)
            paras = [p for p in re.split(r"\n\s*\n", text) if p.strip() and not p.startswith("# ")]
            best = max(paras, key=lambda p: len(ws & words(p)), default="")
            out.append({"title": n["title"], "area": n["area"], "note": n["rel"], "passage": best.strip()[:400]})
        return {"notes": out}

    return ctx.step("look", look)


# ------------------------------------------------------------------ tidy

def lifehub_notes(ctx: Context) -> list[dict]:
    base = str(ctx.config.get("lifehub_url") or "").rstrip("/")
    key = ctx.secrets.get("LIFEHUB_API_KEY")
    if not base or not key:
        return []
    try:
        status, raw = ctx.http.request("GET", f"{base}/api/items?list=learn",
                                       headers={"X-API-Key": key, "Accept": "application/json"})
    except OSError:
        return []  # Life Hub is off: next time
    if status >= 400:
        ctx.log.warning("life hub refused", extra={"status": status})
        return []
    rows = json.loads(raw or b"[]")
    return [{"id": r["id"], "uid": r.get("uid"), "text": r.get("notes") or r.get("name") or "",
             "enhance": r.get("priority") == 1, "from": "diary" if r.get("store") == "diary" else "lifehub"}
            for r in rows if not r.get("deleted")]


def take_from_lifehub(ctx: Context, row: dict) -> dict:
    """Write one Life Hub note into the Inbox, then remove it from Life Hub (so it's taken once)."""
    if ctx.dry_run:
        return {"would_take": row["uid"]}
    seen = ctx.store.get("lifehub_taken") or []
    if row["uid"] not in seen and row["text"].strip():
        text, meta = row["text"], {"source": row.get("from") or "lifehub", "enhance": row["enhance"]}
        if meta["source"] == "diary" and ctx.config.get("write_spoken", True):  # said to Ari in your diary
            up = write_up(ctx, text)
            if up:
                text, meta["title"], meta["topic"] = up["body"], up["title"], up["topic"]
        write_capture(ctx, text, meta)
        ctx.store.set("lifehub_taken", (seen + [row["uid"]])[-1000:])
    base = str(ctx.config.get("lifehub_url")).rstrip("/")
    ctx.http.request("DELETE", f"{base}/api/items/{row['id']}",
                         headers={"X-API-Key": ctx.secrets.get("LIFEHUB_API_KEY") or ""})
    return {"taken": row["uid"]}


def conflicts(ctx: Context) -> list[str]:
    """Syncthing's conflict copies ("name.sync-conflict-<date>-<time>-<device>.md") you haven't heard about."""
    if not ctx.files.is_dir(vault()):
        return []
    told = set(ctx.store.get("conflicts_told") or [])
    found = [Path(p).relative_to(vault()).as_posix() for p, _s, _m in ctx.files.walk(vault(), limit=50_000)
             if ".sync-conflict-" in Path(p).name]
    new = [f for f in found if f not in told]
    if new:
        ctx.notify("Notes vault: a note was changed on two devices",
                   "Syncthing kept both copies: " + ", ".join(new[:5]) +
                   ". Open them in Obsidian, keep what you want and delete the conflict copy.")
        ctx.store.set("conflicts_told", sorted(told | set(new))[-500:])
    return new


def to_enhance(ctx: Context) -> list[str]:
    out = []
    for n in topic_notes(ctx):
        if time.time() - n["mtime"] < RECENT:
            continue
        meta, body = split_fm(ctx.files.read_text(n["path"], limit=200_000))
        if marked(meta, body):
            out.append(n["rel"])
    return out


def leftovers(ctx: Context) -> list[str]:
    box = vault() / INBOX
    if not ctx.files.is_dir(box):
        return []
    pending = {k for k, at in (ctx.store.get("pending") or {}).items() if time.time() - at < 2 * 86400}
    left = set(ctx.store.get("left") or [])
    out = []
    for p in ctx.files.list(box, "*.md"):
        name = Path(p).name
        if name in pending or name in left or name.startswith("."):
            continue
        if time.time() - ctx.files.stat(p).st_mtime >= LEFTOVER:
            out.append(p)
    return out


@workflow(PLUGIN, "tidy")
def tidy(ctx: Context):
    if not ctx.files.is_dir(vault()):
        raise PermanentError("no ~/Vault on this machine yet (scripts/vault-setup.ps1 makes it)")
    rows = ctx.step("life hub", lifehub_notes, ctx)
    for r in rows:
        ctx.step(f"take {r['uid']}", take_from_lifehub, ctx, r)
    clashes = ctx.step("conflicts", conflicts, ctx)

    out: dict[str, Any] = {"from_lifehub": len(rows), "conflicts": clashes, "enhanced": [], "filed": [],
                           "problems": []}
    marked_notes = ctx.step("marked", to_enhance, ctx)
    allowed = min(2, ctx.step("budget", budget_left, ctx))  # a few per run: Claude's daily cap is shared
    if len(marked_notes) > allowed:
        out["enhance_waiting"] = len(marked_notes) - allowed
    for i, rel in enumerate(marked_notes[:allowed], 1):
        try:
            out["enhanced"].append(enhance_one(ctx, rel, i))
        except WaitSignal:
            raise
        except Exception as e:  # one note's trouble doesn't stop the rest; it's tried again next time
            out["problems"].append(f"{rel}: {e}"[:300])

    for i, p in enumerate(ctx.step("leftovers", leftovers, ctx)[:5], 1):
        out["filed"].append(file_one(ctx, p, i))
    return out
