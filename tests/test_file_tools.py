"""File tools: PDFs and pictures, new files next to the originals, never a change to them."""

from __future__ import annotations

import importlib.util
import io
from pathlib import Path

import pytest
from PIL import Image

from argus.config import load_config
from argus.context import Argus
from argus.worker import PermanentError, Worker
from test_worker import Server, client, wait_for

pypdf = pytest.importorskip("pypdf")
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("t_file_tools", ROOT / "plugins" / "file-tools" / "plugin.py")
ft = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ft)


def test_paths_and_pages():
    got = ft.paths('"~/Downloads/a.pdf"; /x/b.PDF\n', (".pdf",))
    assert [p.name for p in got] == ["a.pdf", "b.PDF"]
    with pytest.raises(PermanentError, match="isn't a pdf"):
        ft.paths("/x/a.docx", (".pdf",))
    assert ft.page_list("2-4, 7", 10) == [1, 2, 3, 6] and ft.page_list("9-end", 10) == [8, 9]
    with pytest.raises(PermanentError, match="has 3 pages"):
        ft.page_list("2-5", 3)


def pdf(n: int) -> bytes:
    w = pypdf.PdfWriter()
    for _ in range(n):
        w.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def test_the_tools_for_real(tmp_path, monkeypatch):
    home = tmp_path / "home"
    docs = home / "Documents"
    docs.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    (docs / "a.pdf").write_bytes(pdf(2))
    original = (docs / "a.pdf").read_bytes()
    (docs / "b.pdf").write_bytes(pdf(3))
    Image.new("RGB", (3000, 2000), (200, 30, 30)).save(docs / "photo.jpg")
    (tmp_path / "argus.yaml").write_text(
        "logging:\n  file: null\njobs:\n  watchdog_interval_seconds: 0.1\n"
        f"plugins:\n  dirs: ['{(ROOT / 'plugins').as_posix()}']\n  live: [file-tools]\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    with Server(Argus(load_config(tmp_path / "argus.yaml")).open()) as srv:
        cl = client(srv.url)
        w = Worker(cl, "pc", capabilities=["desktop"], watch_folders=False)
        w.register()
        assert "file-tools" in w.plugins, w.plugin_errors

        def call(workflow, **inp):
            job = cl.post("/jobs", {"plugin": "file-tools", "workflow": workflow, "needs": ["desktop"], "input": inp})
            w.run_once(wait=1)
            j = wait_for(lambda: (x := cl.get(f"/jobs/{job['id']}"))["state"] in ("succeeded", "dead") and x)
            assert j["state"] == "succeeded", j["error"]
            return j["result"]

        m = call("merge_pdfs", files=f"{docs / 'a.pdf'}; {docs / 'b.pdf'}")
        assert m["pages"] == 5 and len(pypdf.PdfReader(m["made"]).pages) == 5
        p = call("pdf_pages", file=str(docs / "b.pdf"), pages="2-3")
        assert Path(p["made"]).name == "b pages 2-3.pdf" and len(pypdf.PdfReader(p["made"]).pages) == 2
        s = call("shrink_images", files=str(docs / "photo.jpg"), max_side=800)["made"][0]
        assert max(Image.open(s["made"]).size) == 800 and s["kb_after"] < s["kb_before"]
        i = call("images_to_pdf", files=str(docs / "photo.jpg"))
        assert len(pypdf.PdfReader(i["made"]).pages) == 1
        assert (docs / "a.pdf").read_bytes() == original  # the originals are never changed
        tools = {t["name"]: t for t in cl.get("/tools")}
        assert all(tools[n]["risky"] for n in ("merge_pdfs", "images_to_pdf", "pdf_pages", "shrink_images"))
