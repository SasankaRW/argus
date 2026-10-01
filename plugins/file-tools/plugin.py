"""File tools: merge PDFs, pictures into a PDF, pages out of a PDF, smaller copies of pictures.

Every tool makes a new file next to the original ("name merged.pdf", "name (small).jpg"); nothing is changed or
deleted, and Ari asks before each one. Needs pypdf and Pillow (pip install -e .[plugins]).
"""

from __future__ import annotations

import io
import os
import re
from pathlib import Path

from argus.worker import Context, PermanentError, workflow

PLUGIN = "file-tools"
MAX_FILES = 60
PICS = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff")


def paths(text: str, exts: tuple[str, ...]) -> list[Path]:
    """Full paths from Ari's text (one per line or ;-separated, quotes allowed), checked for type and count."""
    out = []
    for raw in re.split(r"[;\n]", str(text or "")):
        p = raw.strip().strip('"').strip("'").strip()
        if not p:
            continue
        path = Path(os.path.expanduser(p))
        if path.suffix.lower() not in exts:
            raise PermanentError(f"{path.name} isn't a {'/'.join(e.lstrip('.') for e in exts[:3])} file")
        out.append(path)
    if not out:
        raise PermanentError("which files?")
    if len(out) > MAX_FILES:
        raise PermanentError(f"at most {MAX_FILES} files at once")
    return out


def page_list(spec: str, total: int) -> list[int]:
    """'2-5, 7' -> [1, 2, 3, 4, 6] (0-based), within the document."""
    out: list[int] = []
    for part in re.split(r"[,\s]+", spec.strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)(?:-(\d+|end))?", part.lower())
        if not m:
            raise PermanentError(f"pages like 2-5 or 1,3,7 (not {part!r})")
        a = int(m.group(1))
        b = total if m.group(2) == "end" else int(m.group(2) or a)
        if a < 1 or b < a or b > total:
            raise PermanentError(f"the PDF has {total} pages")
        out.extend(range(a - 1, b))
    if not out:
        raise PermanentError("which pages?")
    return out


def _pypdf():
    try:
        import pypdf  # type: ignore[import-not-found]
    except ImportError:
        raise PermanentError("needs pypdf (pip install -e .[plugins])") from None
    return pypdf


def _pil():
    try:
        from PIL import Image  # type: ignore[import-not-found]
    except ImportError:
        raise PermanentError("needs Pillow (pip install -e .[plugins])") from None
    return Image


def _pdf_bytes(writer) -> bytes:
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


@workflow(PLUGIN, "merge_pdfs")
def merge_pdfs(ctx: Context):
    files = paths(ctx.input.get("files"), (".pdf",))
    if len(files) < 2:
        raise PermanentError("give at least two PDFs to merge")

    def go():
        pypdf = _pypdf()
        w = pypdf.PdfWriter()
        pages = 0
        for f in files:
            r = pypdf.PdfReader(io.BytesIO(ctx.files.read_bytes(f)))
            for page in r.pages:
                w.add_page(page)
                pages += 1
        where = ctx.files.write_bytes(files[0].with_name(f"{files[0].stem} merged.pdf"), _pdf_bytes(w))
        return {"made": where, "pages": pages, "from": len(files), "dry_run": ctx.dry_run}

    return ctx.step("merge", go)


@workflow(PLUGIN, "images_to_pdf")
def images_to_pdf(ctx: Context):
    files = paths(ctx.input.get("files"), PICS)

    def go():
        image = _pil()
        pics = []
        for f in files:
            im = image.open(io.BytesIO(ctx.files.read_bytes(f)))
            pics.append(im.convert("RGB"))
        buf = io.BytesIO()
        pics[0].save(buf, format="PDF", save_all=True, append_images=pics[1:], resolution=150)
        where = ctx.files.write_bytes(files[0].with_name(f"{files[0].stem} pictures.pdf"), buf.getvalue())
        return {"made": where, "pages": len(pics), "dry_run": ctx.dry_run}

    return ctx.step("make", go)


@workflow(PLUGIN, "pdf_pages")
def pdf_pages(ctx: Context):
    (f,) = paths(ctx.input.get("file"), (".pdf",))[:1]
    spec = str(ctx.input.get("pages") or "")

    def go():
        pypdf = _pypdf()
        r = pypdf.PdfReader(io.BytesIO(ctx.files.read_bytes(f)))
        idx = page_list(spec, len(r.pages))
        w = pypdf.PdfWriter()
        for i in idx:
            w.add_page(r.pages[i])
        tag = re.sub(r"[^\d,-]", "", spec.replace(" ", ""))[:20]
        where = ctx.files.write_bytes(f.with_name(f"{f.stem} pages {tag}.pdf"), _pdf_bytes(w))
        return {"made": where, "pages": len(idx), "dry_run": ctx.dry_run}

    return ctx.step("pages", go)


@workflow(PLUGIN, "shrink_images")
def shrink_images(ctx: Context):
    files = paths(ctx.input.get("files"), PICS)
    side = max(200, min(8000, int(ctx.input.get("max_side") or 1600)))
    out = []
    for i, f in enumerate(files):
        def one(f=f) -> dict:
            image = _pil()
            im = image.open(io.BytesIO(ctx.files.read_bytes(f)))
            before = ctx.files.stat(f).st_size
            im.thumbnail((side, side))
            buf = io.BytesIO()
            png = f.suffix.lower() == ".png"
            (im if png else im.convert("RGB")).save(buf, format="PNG" if png else "JPEG", quality=82, optimize=True)
            ext = ".png" if png else ".jpg"
            where = ctx.files.write_bytes(f.with_name(f"{f.stem} (small){ext}"), buf.getvalue())
            return {"made": where, "kb_before": round(before / 1024), "kb_after": round(len(buf.getvalue()) / 1024)}

        out.append(ctx.step(f"shrink {i + 1}", one))
    return {"made": out, "dry_run": ctx.dry_run}
