"""Downloads organizer (port of the standalone script to an Argus plugin).

A file that lands in Downloads and stops changing for 2 minutes becomes a `file` job; a nightly `sort` sweep (also
"Sort Downloads now" in Helios) picks up anything left over. Each job:

1. Groups the file with its siblings: names reduced to what they are about (`resume_v2.pdf`, `resume (1).pdf` ->
   "resume"), so versions and copies stay together.
2. Decides where the group goes, cheapest first:
   - a folder with that name already exists inside a category (Projects/DeepLense) -> there, no model;
   - otherwise the models pick a category (T1, T2 if T1's answer fails the check); the extension decides only
     when no model gives a valid answer;
   - two or more siblings get their own subfolder (`Documents/lecture/`).
3. Moves the files (never overwriting; every move is an event, so it can be undone). In dry-run (until the plugin
   is listed under `plugins.live`) it only records what it would do.

Categories, guidance, examples and the extension fallback live in rules.yaml.
"""

from __future__ import annotations

import os
import re
import stat
import time
import urllib.parse
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from argus.models import EscalationExhausted
from argus.worker import Context, PermanentError, workflow

PLUGIN = "downloads-organizer"
DEFAULT_RULES_TEXT = (Path(__file__).parent / "rules.yaml").read_text(encoding="utf-8")


def check_rules(r: object) -> dict:
    """The rules, checked, or PermanentError saying what to fix (shown on the job in Helios)."""
    def bad(msg: str):
        raise PermanentError(f"sorting rules: {msg} (fix them in Helios: Downloads box > Edit rules)")

    if not isinstance(r, dict):
        bad("expected sections like categories:, guidance:, examples:")
    cats = r.get("categories")
    if not isinstance(cats, dict) or not cats or not all(isinstance(k, str) and k.strip() for k in cats):
        bad("categories: needs at least one entry like  Documents: PDFs, Word files, ...")
    for k in cats:
        if SAFE_NAME.search(k) or k != k.strip():
            bad(f"category {k!r} can't be a folder name")
    for sect in ("aliases", "merge", "examples"):
        m = r.get(sect) or {}
        if not isinstance(m, dict):
            bad(f"{sect}: should be a list of  name: Category")
        wrong = [f"{k} -> {v}" for k, v in m.items() if v not in cats]
        if wrong:
            bad(f"{sect}: {', '.join(wrong[:3])} - the target must be one of the categories")
    if not isinstance(r.get("guidance") or [], list):
        bad("guidance: should be a list of lines starting with -")
    for sect in ("extension_fallback", "by_extension"):
        fb = r.get(sect) or {}
        if not isinstance(fb, dict) or any(k not in cats or not isinstance(v, list) for k, v in fb.items()):
            bad(f"{sect}: should be  Category: [.ext, .ext]  with real categories")
    dests = r.get("destinations") or {}
    if not isinstance(dests, dict) or any(k not in cats or not isinstance(v, str) or not v.strip()
                                          for k, v in dests.items()):
        bad("destinations: should be  Category: folder  (e.g. Videos: ~/Videos) with real categories")
    for k in (r.get("merge") or {}):
        if k in cats:
            bad(f"merge: {k} is itself a category")
    return r


SAFE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RULES: dict = check_rules(yaml.safe_load(DEFAULT_RULES_TEXT))
CATEGORIES: dict[str, str] = RULES["categories"]


def activate(ctx: Context) -> str:
    """Use the rules saved in Helios, if any, else rules.yaml. Called at the start of every job, so an edit
    counts from the next job on. Returns where they came from."""
    global RULES, CATEGORIES
    text = ctx.store.get("rules") if ctx.store is not None else None
    if text:
        try:
            r = yaml.safe_load(text)
        except yaml.YAMLError as e:
            raise PermanentError(f"sorting rules are not valid YAML: {str(e)[:200]}") from None
        RULES, source = check_rules(r), "Helios"
    else:
        RULES, source = check_rules(yaml.safe_load(DEFAULT_RULES_TEXT)), "rules.yaml"
    CATEGORIES = RULES["categories"]
    return source

SKIP_EXT = {".crdownload", ".part", ".partial", ".tmp", ".download", ".!ut", ".opdownload"}
SKIP_NAMES = {"desktop.ini", "thumbs.db", ".ds_store"}
TEXTY = {".txt", ".md", ".csv", ".json", ".log", ".html", ".htm", ".xml", ".yml", ".yaml", ".py", ".js", ".ts",
         ".java", ".c", ".cpp", ".sql", ".ini", ".cfg"}
BAD_WIN_CHARS = r'[<>:"/\\|?*\x00-\x1f]'


# ------------------------------------------------------------------ grouping (pure functions, tested directly)

VERSION_TAIL = re.compile(
    r"[\s._\-]*(?:\(\d+\)|v\d+(?:\.\d+)*|ver\d+|rev\d+|final|copy|draft|latest|new|old"
    r"|part\s*\d+|pt\d+|page\s*\d+|\d{1,4})$", re.I)
DATEISH = re.compile(r"\b(20\d{2}|19\d{2})[-_.]?\d{1,2}[-_.]?\d{1,2}\b")
TRIM = re.compile(r"[\s._\-(\[]+$")


def norm_stem(name: str) -> str:
    """What a filename is about: resume_v2.pdf, resume (1).pdf, resume-final.pdf -> 'resume'."""
    stem = DATEISH.sub(" ", os.path.splitext(name)[0])
    for _ in range(4):
        new = VERSION_TAIL.sub("", stem)
        if new == stem:
            break
        stem = new
    stem = re.sub(r"[^A-Za-z0-9]+", " ", stem).strip().lower()
    return re.sub(r"\s+", " ", stem)


def safe_folder(name: str) -> str:
    return re.sub(BAD_WIN_CHARS, "", name).strip(" .")[:60] or "Group"


def group_label(names: list[str], key: str | None = None) -> str:
    """A folder name from what the names share: lecture_07.pdf + lecture_08.pdf -> 'lecture'."""
    stems = [os.path.splitext(n)[0] for n in names]
    pre = TRIM.sub("", os.path.commonprefix(stems))
    if pre and all(len(s) > len(pre) and s[len(pre)].isdigit() for s in stems):  # prefix ends mid-number
        pre = TRIM.sub("", re.sub(r"\d+$", "", pre))
        pre = TRIM.sub("", re.sub(r"[\s._\-]*(?:v|ver|rev|part|pt|page|no|ep)$", "", pre, flags=re.I))
    if len(pre) < 3:
        pre = TRIM.sub("", VERSION_TAIL.sub("", stems[0]))
    if len(pre) < 3 and key:
        pre = key.title()
    return safe_folder(pre)


def build_groups(files: list[dict], min_chars: int = 5) -> list[dict]:
    """[{name, ...}] -> [{key, label, members}] sorted by first name. Short names are never grouped."""
    buckets: dict[str, list[dict]] = {}
    for f in files:
        key = norm_stem(f["name"])
        if len(key.replace(" ", "")) < min_chars:
            key = "\x00solo:" + f["name"].lower()
        buckets.setdefault(key, []).append(f)
    out = []
    for key, members in buckets.items():
        solo = key.startswith("\x00solo:")
        out.append({"key": None if solo else key,
                    "label": None if solo or len(members) < 2 else group_label([m["name"] for m in members], key),
                    "members": members})
    return sorted(out, key=lambda g: g["members"][0]["name"].lower())


def match_existing(index: dict[str, tuple[str, str]], key: str | None) -> tuple[str, str] | None:
    """The longest existing folder name this group's name starts with -> (category, folder)."""
    if not key:
        return None
    best = None
    for k, v in index.items():
        if (key == k or key.startswith(k + " ")) and (best is None or len(k) > len(best[0])):
            best = (k, v)
    return best[1] if best else None


def normalise(cat: str | None) -> str | None:
    if not cat:
        return None
    c = cat.strip().lower()
    for k in CATEGORIES:
        if c == k.lower():
            return k
    for a, target in (RULES.get("aliases") or {}).items():
        if c == a.lower() and target in CATEGORIES:
            return target
    return None


def by_type(ext: str) -> str | None:
    """rules: by_extension - file types that always go to one category, decided in code (no model)."""
    for cat, exts in (RULES.get("by_extension") or {}).items():
        if ext.lower() in [e.lower() for e in exts] and cat in CATEGORIES:
            return cat
    return None


def by_extension(ext: str) -> str | None:
    for cat, exts in (RULES.get("extension_fallback") or {}).items():
        if ext in exts and cat in CATEGORIES:
            return cat
    return None


# ------------------------------------------------------------------ the models

class Placement(BaseModel):
    category: str = Field(description="exactly one of the category names")
    reason: str = Field("", description="at most 8 words")


LEARNED_MAX = 50  # corrections kept as examples (newest win)


def playbook(learned: dict[str, str] | None = None) -> str:
    """The instructions, with rules.yaml's examples plus the corrections you made with the Wrong button."""
    mine = {f: c for f, c in (learned or {}).items() if c in CATEGORIES}  # a category since removed: left out
    examples = {**(RULES.get("examples") or {}), **mine}
    return ("Sort a download into exactly one category.\n\nCATEGORIES (choose one, spelled exactly):\n"
            + "\n".join(f"- {k}: {v}" for k, v in CATEGORIES.items())
            + "\n\nRULES:\n" + "\n".join(f"- {g}" for g in RULES.get("guidance") or [])
            + "\n\nEXAMPLES:\n" + "\n".join(f'- "{f}" -> {c}' for f, c in examples.items())
            + '\n\nAnswer as JSON: {"category": "<one name from the list>", "reason": "<max 8 words>"}')


PLAYBOOK = playbook()


def classify(ctx: Context, names: list[str], ext: str, preview: str) -> tuple[str | None, str, str | None]:
    """-> (category, reason, tier). The models first; the extension only when no model gives a valid answer."""

    def check(p: Placement, _inp) -> str | None:
        if normalise(p.category) is None:
            return f"{p.category!r} is not a category; use one of: {', '.join(CATEGORIES)}"
        return None

    task: dict = {"files": names[:8]}
    if len(names) > 1:
        task["note"] = f"these {len(names)} files belong together and must all go to the SAME category"
    if preview:
        task["content_preview"] = preview
    try:
        p = ctx.llm(playbook(getattr(ctx, "learned", None)), task, schema=Placement, check=check)
        return normalise(p.category), (p.reason or "model")[:120], ctx.last_answer.tier
    except EscalationExhausted as e:
        cat = by_extension(ext)
        if cat:
            return cat, "extension fallback - models were unclear", None
        return None, f"no usable category ({str(e)[:80]})", None


# ------------------------------------------------------------------ planning and moving

def downloads(ctx: Context) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(ctx.config.get("downloads_dir") or "~/Downloads"))))


def candidates(ctx: Context, root: Path, min_age: float) -> list[dict]:
    """Loose files in the Downloads root (never inside the category folders)."""
    now = time.time()
    out = []
    for p in ctx.files.list(root):
        name = os.path.basename(p)
        low = name.lower()
        ext = os.path.splitext(low)[1]
        if low in SKIP_NAMES or low.startswith("~$") or name.startswith(".") or ext in SKIP_EXT:
            continue
        try:
            st = ctx.files.stat(p)
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        out.append({"path": p, "name": name, "ext": ext, "size": st.st_size, "young": now - st.st_mtime < min_age})
    return out


def folder_for(root: Path, cat: str) -> Path:
    """Where a category's files go: Downloads/<category>, or the folder set under destinations: (e.g. Videos in
    your own Videos folder)."""
    dest = (RULES.get("destinations") or {}).get(cat)
    return Path(os.path.expandvars(os.path.expanduser(str(dest)))) if dest else root / cat


def folder_index(ctx: Context, root: Path) -> dict[str, tuple[str, str]]:
    """Subfolders already inside the category folders: Projects/DeepLense -> 'deeplense'."""
    idx: dict[str, tuple[str, str]] = {}
    if not ctx.config.get("match_existing_folders", True):
        return idx
    for cat in CATEGORIES:
        d = folder_for(root, cat)
        if not ctx.files.exists(d) or not ctx.files.is_dir(d):
            continue
        for sub in ctx.files.list(d):
            if ctx.files.is_dir(sub):
                k = norm_stem(os.path.basename(sub))
                if len(k.replace(" ", "")) >= 4:
                    idx[k] = (cat, sub)
    return idx


def preview(ctx: Context, f: dict) -> str:
    if f["ext"] not in TEXTY:
        return ""
    try:
        return ctx.files.read_text(f["path"], limit=400).replace("\n", " ").strip()
    except OSError:
        return ""


def plan_groups(ctx: Context, root: Path, groups: list[dict], limit: int) -> dict:
    """Decide where each group goes. Only files that are old enough are moved; young siblings still count
    towards the group (their own job moves them later, to the same place)."""
    if ctx.store is not None:
        ctx.store.set("choices", list(CATEGORIES))  # for the Wrong button in Helios
        ctx.learned = ctx.store.get("learned", {}) or {}
    idx = folder_index(ctx, root)
    min_group = int(ctx.config.get("min_group_size", 2))
    moves: list[dict] = []
    skipped: list[dict] = []
    note = None
    for grp in groups:
        ready = [f for f in grp["members"] if not f["young"]]
        if not ready:
            continue
        if len(moves) >= limit:
            note = f"hit max_files_per_run ({limit}); the rest waits for the next run"
            break
        types = {by_type(f["ext"]) for f in ready}
        hit = match_existing(idx, grp["key"])
        tier = None
        if len(types) == 1 and None not in types:  # a video is a video: straight to Videos, no guessing
            cat = types.pop()
            dest = folder_for(root, cat)
            reason = f"file type ({', '.join(sorted({f['ext'] for f in ready}))})"
            if grp["label"] and len(grp["members"]) >= int(ctx.config.get("min_group_size", 2)):
                dest = dest / grp["label"]
        elif hit:
            cat, dest = hit[0], Path(hit[1])
            reason = f"goes with existing {cat}/{os.path.basename(hit[1])}"
        else:
            first = ready[0]
            cat, reason, tier = classify(ctx, [f["name"] for f in grp["members"]], first["ext"], preview(ctx, first))
            if cat is None:
                skipped += [{"name": f["name"], "reason": reason} for f in ready]
                continue
            dest = folder_for(root, cat)
            if grp["label"] and len(grp["members"]) >= min_group:
                dest = dest / grp["label"]
                reason = f"{reason} (grouped: {len(grp['members'])} files share a name)"
        for f in ready:
            moves.append({"src": f["path"], "dst": str(dest / f["name"]), "name": f["name"], "category": cat,
                          "folder": os.path.relpath(dest, root) if _inside(dest, root) else str(dest),
                          "reason": reason, "tier": tier})
    return {"moves": moves, "skipped": skipped, "note": note}


def apply(ctx: Context, plan: dict) -> dict:
    done, gone = [], []
    for m in plan["moves"]:
        if not os.path.exists(m["src"]):  # moved by an earlier try, or by you
            gone.append(m["name"])
            continue
        done.append({"name": m["name"], "to": ctx.files.move(m["src"], m["dst"]), "category": m["category"]})
    ctx.emit("sorted", moved=len(done), skipped=len(plan["skipped"]), dry_run=ctx.dry_run)
    if ctx.dry_run:  # say it plainly: nothing moved
        return {"mode": "dry-run: nothing was moved (add downloads-organizer to plugins.live to let it move files)",
                "would_move": done, "skipped": plan["skipped"], "dry_run": True, "note": plan["note"]}
    return {"moved": done, "skipped": plan["skipped"], "gone": gone, "dry_run": False, "note": plan["note"]}


# ------------------------------------------------------------------ workflows

@workflow(PLUGIN, "sort")
def sort(ctx: Context):
    """Sweep the whole Downloads root."""
    activate(ctx)
    root = downloads(ctx)

    def make_plan():
        files = candidates(ctx, root, float(ctx.config.get("min_age_seconds", 120)))
        return plan_groups(ctx, root, build_groups(files), int(ctx.config.get("max_files_per_run", 60)))

    plan = ctx.step("plan", make_plan)
    return ctx.step("move", apply, ctx, plan)


def _inside(p: str | Path, root: str | Path) -> bool:
    try:
        Path(os.path.realpath(p)).relative_to(os.path.realpath(root))
        return True
    except ValueError:
        return False


def _same(a: str | Path, b: str | Path) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


@workflow(PLUGIN, "file")
def file(ctx: Context):
    """One finished download (from the folder watch): it and its settled siblings."""
    activate(ctx)
    root = downloads(ctx)
    path = str(ctx.input.get("path") or "")
    if not path:
        raise PermanentError("no path in the job input")

    def make_plan():
        if not os.path.exists(path):
            return {"moves": [], "skipped": [], "note": "already gone"}
        if not _same(os.path.dirname(path), root):
            return {"moves": [], "skipped": [], "note": "not in the Downloads root"}
        files = candidates(ctx, root, float(ctx.config.get("min_age_seconds", 120)))
        name = os.path.basename(path)
        for f in files:
            if f["name"] == name:
                f["young"] = False  # the watcher already waited for it to settle
        mine = [g for g in build_groups(files) if any(f["name"] == name for f in g["members"])]
        return plan_groups(ctx, root, mine, int(ctx.config.get("max_files_per_run", 60)))

    plan = ctx.step("plan", make_plan)
    return ctx.step("move", apply, ctx, plan)


@workflow(PLUGIN, "undo")
def undo(ctx: Context):
    """Put a moved file back: input {"from": where it is now, "to": where it was}."""
    src, dst = str(ctx.input.get("from") or ""), str(ctx.input.get("to") or "")
    if not src or not dst:
        raise PermanentError('undo needs {"from": ..., "to": ...}')
    if not os.path.exists(src):
        raise PermanentError(f"{src} is no longer there")
    return {"back_to": ctx.step("undo", ctx.files.move, src, dst), "dry_run": ctx.dry_run}


@workflow(PLUGIN, "correct")
def correct(ctx: Context):
    """The Wrong button: input {"from": where the file is now, "value": the right category}. Moves it there (keeping
    its group subfolder) and remembers the answer as an example for the models."""
    activate(ctx)
    src, value = str(ctx.input.get("from") or ""), normalise(str(ctx.input.get("value") or ""))
    if value is None:
        raise PermanentError(f"{ctx.input.get('value')!r} is not a category ({', '.join(CATEGORIES)})")
    if not os.path.exists(src):
        raise PermanentError(f"{src} is no longer there")
    root = downloads(ctx)
    parent = Path(os.path.dirname(src))
    cat_dirs = {os.path.normcase(str(folder_for(root, c))) for c in CATEGORIES}
    sub = parent.name if os.path.normcase(str(parent.parent)) in cat_dirs else ""  # keep its group subfolder
    dest = folder_for(root, value) / sub / os.path.basename(src)
    moved = ctx.step("move", ctx.files.move, src, str(dest))

    def learn():
        learned = ctx.store.get("learned", {}) or {}
        learned.pop(os.path.basename(src), None)
        learned[os.path.basename(src)] = value
        ctx.store.set("learned", dict(list(learned.items())[-LEARNED_MAX:]))
        return len(learned)

    kept = ctx.step("learn", learn)
    return {"moved_to": moved, "category": value, "examples_learned": min(kept, LEARNED_MAX), "dry_run": ctx.dry_run}


def _title_name(text: str, fallback: str) -> str:
    words = re.sub(r"[^A-Za-z0-9 ]+", " ", text).split()[:8]
    return safe_folder(" ".join(words)) if words else fallback


@workflow(PLUGIN, "take")
def take(ctx: Context):
    """Something shared from the phone: files, a link (saved as a .url shortcut) or text (a .txt). Saved to the
    Downloads root, then sorted straight away like any other download."""
    activate(ctx)
    root = downloads(ctx)
    inp = ctx.input

    def save():
        saved = []
        for f in inp.get("files") or []:
            if ctx.shared is None:
                raise PermanentError("no shared files available to this job")
            saved.append(ctx.files.write_bytes(root / f["name"], ctx.shared(f["name"])))
        if inp.get("url"):
            name = _title_name(inp.get("title") or "", urllib.parse.urlparse(inp["url"]).hostname or "link")
            saved.append(ctx.files.write_text(root / f"{name}.url", f"[InternetShortcut]\r\nURL={inp['url']}\r\n"))
        if inp.get("text") and not inp.get("url"):
            name = _title_name(inp.get("title") or inp["text"], "shared text")
            body = inp["text"] + (f"\n\n{inp['note']}" if inp.get("note") else "")
            saved.append(ctx.files.write_text(root / f"{name}.txt", body))
        return saved

    saved = ctx.step("save", save)
    if ctx.dry_run:
        return {"saved": saved, "mode": "dry-run: nothing was saved or sorted", "dry_run": True}

    def make_plan():
        names = {os.path.basename(p) for p in saved}
        files = candidates(ctx, root, float(ctx.config.get("min_age_seconds", 120)))
        for f in files:
            if f["name"] in names:
                f["young"] = False  # complete: we just wrote it
        mine = [g for g in build_groups(files) if any(f["name"] in names for f in g["members"])]
        return plan_groups(ctx, root, mine, int(ctx.config.get("max_files_per_run", 60)))

    plan = ctx.step("plan", make_plan)
    return {"saved": saved, **ctx.step("move", apply, ctx, plan)}


@workflow(PLUGIN, "tidy")
def tidy(ctx: Context):
    """Fold second names into the real category folders (rules: merge), e.g. Pictures into Images: everything
    inside moves over (never overwriting), then the empty folder goes. Every move can be undone."""
    source = activate(ctx)
    root = downloads(ctx)

    def plan():
        out = []
        for src, dst in (RULES.get("merge") or {}).items():
            d = root / src
            if ctx.files.exists(d) and ctx.files.is_dir(d) and not _same(d, folder_for(root, dst)):
                out.append({"from": str(d), "to": str(folder_for(root, dst)), "items": ctx.files.list(d)})
        for cat in (RULES.get("destinations") or {}):  # a category that now lives elsewhere: bring its old folder
            d = root / cat
            if ctx.files.exists(d) and ctx.files.is_dir(d) and not _same(d, folder_for(root, cat)):
                out.append({"from": str(d), "to": str(folder_for(root, cat)), "items": ctx.files.list(d)})
        return out

    def misplaced():
        """Files whose type has a fixed category (by_extension) sitting in another category's folder, e.g. a
        video in Audio or Misc: -> [{from, to}], keeping a group subfolder's name."""
        out = []
        for cat in CATEGORIES:
            base = folder_for(root, cat)
            if not ctx.files.exists(base) or not ctx.files.is_dir(base):
                continue
            for p in ctx.files.list(base):
                inner = ctx.files.list(p) if ctx.files.is_dir(p) else [p]
                for f in inner:
                    want = by_type(os.path.splitext(f)[1].lower())
                    if want and want != cat and not ctx.files.is_dir(f):
                        sub = os.path.basename(p) if f != p else ""
                        out.append({"from": f, "to": str(folder_for(root, want) / sub / os.path.basename(f))})
        return out

    merges = ctx.step("plan", plan)

    def run():
        done = []
        for m in merges:
            for item in m["items"]:
                if os.path.exists(item):
                    ctx.files.move(item, os.path.join(m["to"], os.path.basename(item)))
            removed = ctx.files.remove_empty_dir(m["from"])
            done.append({"folder": os.path.basename(m["from"]), "into": m["to"] if not _inside(m["to"], root)
                         else os.path.basename(m["to"]),
                         "moved": len(m["items"]), "removed": removed})
        return done

    done = ctx.step("merge", run)
    strays = ctx.step("find misplaced", misplaced)  # after the merge: a video that was in Media is in Audio now

    def fix():
        return [{"file": os.path.basename(m["from"]), "to": os.path.relpath(ctx.files.move(m["from"], m["to"]), root)}
                for m in strays if os.path.exists(m["from"])]

    moved = ctx.step("move misplaced", fix)
    ctx.emit("tidied", folders=len(done), files=len(moved), dry_run=ctx.dry_run)
    return {"merged": done, "misplaced": moved, "rules": source, "dry_run": ctx.dry_run,
            "note": None if done or moved else "nothing to tidy"}
