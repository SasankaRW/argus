"""My files (for Ari): an index of your documents, notes and projects, so Ari answers from your own files first.

`index` (every night at 2:15, or "Index my files now"): walks `folders`, reads what's new or changed (text,
Markdown, code, Word .docx, PDF, HTML), cuts it into passages and keeps them in a local SQLite index on this PC
(data/plugins/knowledge/index.db) with full-text search, plus a vector per passage from the local embedding model
(`embed_model`, nomic-embed-text; skipped when not pulled) for search by meaning. `names_only` folders
(Downloads, Pictures, ...) are listed by file name only. Nothing leaves the PC.

Ari's tools:
- search_my_files(query): the best passages (words and meaning together), each with its file;
- find_file(name): files whose name matches, newest first.
"""

from __future__ import annotations

import html
import os
import re
import sqlite3
import time
import zipfile
from pathlib import Path

from argus.worker import Context, workflow

PLUGIN = "knowledge"
TEXT = {".txt", ".md", ".markdown", ".rst", ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".c", ".cpp", ".h",
        ".cs", ".go", ".rs", ".sql", ".sh", ".ps1", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".json", ".csv",
        ".tex", ".org", ".log"}
SKIP_DIRS = {"node_modules", ".git", ".venv", "venv", "__pycache__", "dist", "build", ".next", ".cache", "target",
             ".idea", ".vscode", "helios_dist", "site-packages", ".pytest_cache", ".ruff_cache", ".mypy_cache"}
CHUNK = 1200
OVERLAP = 200
MAX_CHARS = 300_000  # per file
BATCH = 32  # passages per embedding call
MIN_SIMILARITY = 0.35  # below this a passage isn't about the question, however it ranks

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, mtime REAL, size INTEGER, kind TEXT, indexed_at REAL);
CREATE VIRTUAL TABLE IF NOT EXISTS passages USING fts5(path UNINDEXED, name, text, tokenize='porter unicode61');
CREATE TABLE IF NOT EXISTS vectors (rowid INTEGER PRIMARY KEY, path TEXT, vec BLOB);
CREATE INDEX IF NOT EXISTS vectors_path ON vectors (path);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def db(ctx: Context) -> sqlite3.Connection:
    d = Path(ctx.data_dir)
    d.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(d / "index.db", timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


# ------------------------------------------------------------------ reading files

def read_text(ctx: Context, path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in TEXT:
        t = ctx.files.read_text(path, limit=MAX_CHARS)
        return t if "\x00" not in t[:2000] else ""  # binary in disguise
    if ext in (".html", ".htm"):
        t = ctx.files.read_text(path, limit=MAX_CHARS)
        t = re.sub(r"(?is)<(script|style).*?</\1>", " ", t)
        return html.unescape(re.sub(r"<[^>]+>", " ", t))
    if ext == ".docx":
        with zipfile.ZipFile(ctx.files._check(path, False)) as z:  # noqa: SLF001 - allowed-folder check
            xml = z.read("word/document.xml").decode("utf-8", "replace")
        xml = re.sub(r"</w:p>", "\n", xml)
        return html.unescape(re.sub(r"<[^>]+>", "", xml))[:MAX_CHARS]
    if ext == ".pdf":
        try:
            from pypdf import PdfReader  # type: ignore[import-not-found]
        except ImportError:
            return ""
        r = PdfReader(str(ctx.files._check(path, False)))  # noqa: SLF001
        out = []
        for page in r.pages[:200]:
            out.append(page.extract_text() or "")
            if sum(map(len, out)) > MAX_CHARS:
                break
        return "\n".join(out)[:MAX_CHARS]
    return ""


def readable(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in TEXT | {".html", ".htm", ".docx", ".pdf"}


def chunks(text: str) -> list[str]:
    text = re.sub(r"[ \t]+", " ", re.sub(r"\n{3,}", "\n\n", text)).strip()
    out, i = [], 0
    while i < len(text):
        piece = text[i:i + CHUNK]
        if i + CHUNK < len(text):  # end on a paragraph or sentence when there is one near the end
            cut = max(piece.rfind("\n\n"), piece.rfind(". "))
            if cut > CHUNK * 0.6:
                piece = piece[:cut + 1]
        if piece.strip():
            out.append(piece.strip())
        if i + len(piece) >= len(text):
            break
        i += max(len(piece) - OVERLAP, CHUNK // 2)
    return out


def walk(ctx: Context, folder: str) -> list[tuple[str, int, float]]:
    root = os.path.expanduser(folder)
    if not os.path.isdir(root):
        return []
    return ctx.files.walk(root, limit=200_000, skip_dirs=SKIP_DIRS)


# ------------------------------------------------------------------ vectors

def _pack(v: list[float]) -> bytes:
    import numpy as np

    a = np.asarray(v, dtype=np.float32)
    n = float(np.linalg.norm(a)) or 1.0
    return (a / n).astype(np.float32).tobytes()


def embed_passages(ctx: Context, conn: sqlite3.Connection, model: str, rows: list[tuple[int, str, str]]) -> int:
    """rows: (rowid, path, text). Returns how many got a vector (0 when the model isn't there)."""
    done = 0
    for i in range(0, len(rows), BATCH):
        part = rows[i:i + BATCH]
        vecs = ctx.embed([t for _, _, t in part], model)
        if vecs is None:
            return done
        conn.executemany("INSERT OR REPLACE INTO vectors (rowid, path, vec) VALUES (?,?,?)",
                         [(rid, p, _pack(v)) for (rid, p, _), v in zip(part, vecs, strict=True)])
        done += len(part)
    return done


# ------------------------------------------------------------------ workflows

@workflow(PLUGIN, "index")
def index(ctx: Context):
    folders = [str(f) for f in ctx.config.get("folders") or []]
    names_only = [str(f) for f in ctx.config.get("names_only") or []]
    max_bytes = int(ctx.config.get("max_file_mb", 8)) * 1024 * 1024
    model = str(ctx.config.get("embed_model") or "").strip()

    def run():
        t0 = time.time()
        conn = db(ctx)
        seen: dict[str, tuple[int, float, str]] = {}
        for f in folders:
            for p, s, m in walk(ctx, f):
                seen[p] = (s, m, "content" if readable(p) and s <= max_bytes else "name")
        for f in names_only:
            for p, s, m in walk(ctx, f):
                seen.setdefault(p, (s, m, "name"))
        known = {r["path"]: (r["mtime"], r["size"], r["kind"]) for r in conn.execute("SELECT * FROM files")}
        gone = [p for p in known if p not in seen]
        changed = [p for p, (s, m, k) in seen.items() if known.get(p) != (m, s, k)]
        new_rows: list[tuple[int, str, str]] = []
        read_ok = failed = 0
        for p in gone + changed:
            conn.execute("DELETE FROM passages WHERE path = ?", (p,))
            conn.execute("DELETE FROM vectors WHERE path = ?", (p,))
        conn.executemany("DELETE FROM files WHERE path = ?", [(p,) for p in gone])
        name_of = {p: os.path.basename(p) for p in changed}
        for n, p in enumerate(changed):
            s, m, kind = seen[p]
            text = ""
            if kind == "content":
                try:
                    text = read_text(ctx, p)
                    read_ok += 1
                except Exception:  # a locked, broken or odd file: its name is still found
                    failed += 1
            parts = chunks(text) if text else [""]
            for c in parts:
                cur = conn.execute("INSERT INTO passages (path, name, text) VALUES (?,?,?)", (p, name_of[p], c))
                if c:
                    new_rows.append((int(cur.lastrowid or 0), p, f"{name_of[p]}\n{c}"))
            conn.execute("INSERT OR REPLACE INTO files (path, mtime, size, kind, indexed_at) VALUES (?,?,?,?,?)",
                         (p, m, s, kind, time.time()))
            if n % 200 == 0:
                conn.commit()
        conn.commit()
        vectors = embed_passages(ctx, conn, model, new_rows) if model and new_rows else 0
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('indexed_at', ?)", (str(time.time()),))
        conn.commit()
        totals = conn.execute("SELECT COUNT(*), SUM(kind = 'content') FROM files").fetchone()
        conn.close()
        return {"files": totals[0], "with_contents": totals[1] or 0, "changed": len(changed), "removed": len(gone),
                "read": read_ok, "unreadable": failed, "passages_added": len(new_rows),
                "with_meaning": vectors, "seconds": round(time.time() - t0, 1)}

    return ctx.step("index", run)


def fts_query(q: str) -> str:
    words = [w for w in re.findall(r"[\w.-]+", q.lower()) if len(w) > 1][:12]
    return " OR ".join(f'"{w}"' for w in words)


@workflow(PLUGIN, "search")
def search(ctx: Context):
    q = str(ctx.input.get("query") or "").strip()
    if not q:
        return {"results": [], "note": "nothing to look for"}
    k = min(int(ctx.input.get("limit") or 6), 12)
    model = str(ctx.config.get("embed_model") or "").strip()

    def run():
        conn = db(ctx)
        if conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0:
            return {"results": [], "note": "the index is empty: run 'Index my files now' first"}
        scores: dict[int, float] = {}
        words = fts_query(q)
        if words:
            for rank, r in enumerate(conn.execute(
                    "SELECT rowid FROM passages WHERE passages MATCH ? ORDER BY bm25(passages, 0, 3, 1) LIMIT 40",
                    (words,))):
                scores[r["rowid"]] = scores.get(r["rowid"], 0) + 1 / (60 + rank)
        if model and conn.execute("SELECT 1 FROM vectors LIMIT 1").fetchone():
            qv = ctx.embed([q], model)
            if qv:
                import numpy as np

                rows = conn.execute("SELECT rowid, vec FROM vectors").fetchall()
                mat = np.frombuffer(b"".join(r["vec"] for r in rows), dtype=np.float32).reshape(len(rows), -1)
                v = np.frombuffer(_pack(qv[0]), dtype=np.float32)
                sims = mat @ v
                for rank, i in enumerate(i for i in np.argsort(-sims)[:40] if sims[i] >= MIN_SIMILARITY):
                    rid = rows[int(i)]["rowid"]
                    scores[rid] = scores.get(rid, 0) + 1 / (60 + rank)
        best = sorted(scores, key=lambda r: -scores[r])[:k * 2]
        out, per_file = [], {}
        for rid in best:
            r = conn.execute("SELECT path, text FROM passages WHERE rowid = ?", (rid,)).fetchone()
            if r is None or per_file.get(r["path"], 0) >= 2 or not r["text"]:
                continue
            per_file[r["path"]] = per_file.get(r["path"], 0) + 1
            out.append({"file": r["path"], "passage": r["text"][:700]})
            if len(out) >= k:
                break
        conn.close()
        return {"results": out} if out else {"results": [], "note": f"nothing in your files about {q!r}"}

    return ctx.step("search", run)


@workflow(PLUGIN, "find")
def find(ctx: Context):
    name = str(ctx.input.get("name") or "").strip().lower()
    if not name:
        return {"files": []}

    def run():
        conn = db(ctx)
        pats = [w for w in re.split(r"[\s*]+", name) if w]
        rows = conn.execute("SELECT path, mtime, size FROM files").fetchall()
        hits = [r for r in rows if all(w in os.path.basename(r["path"]).lower() for w in pats)]
        hits.sort(key=lambda r: -r["mtime"])
        conn.close()
        return {"files": [{"path": r["path"], "modified": time.strftime("%Y-%m-%d", time.localtime(r["mtime"])),
                           "size_kb": round(r["size"] / 1024)} for r in hits[:15]],
                "more": max(0, len(hits) - 15)}

    return ctx.step("find", run)

