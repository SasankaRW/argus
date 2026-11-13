"""The status page is read from the code: version, schema, plugins, routes, tests and big files."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("status", ROOT / "scripts" / "status.py")
status = importlib.util.module_from_spec(spec)
spec.loader.exec_module(status)


def test_the_numbers_come_from_the_code():
    assert status.version() == (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    assert status.schema() == len(list((ROOT / "core/argus/db/migrations").glob("*.sql")))
    ids = {p["id"] for p in status.plugins()}
    assert len(ids) == len(list((ROOT / "plugins").glob("*/plugin.yaml"))) and "advisor" in ids
    assert status.routes() > 100


def test_the_page_lists_what_it_found(monkeypatch):
    monkeypatch.setattr(status, "routes", lambda root=ROOT: 161)
    page = status.render(count_tests=False)
    assert page.startswith("# Argus code status")
    assert "| Tests | not counted |" in page and "| API routes | 161 |" in page
    assert f"`{status.code()['big'][0][1]}`" in page  # the biggest file is listed
    assert "| `advisor` | Money advisor |" in page


def test_a_small_tree(tmp_path):
    (tmp_path / "VERSION").write_text("1.2.3\n", encoding="utf-8")
    (tmp_path / "core/argus/db/migrations").mkdir(parents=True)
    for n in ("0001_init.sql", "0002_more.sql"):
        (tmp_path / "core/argus/db/migrations" / n).write_text("", encoding="utf-8")
    (tmp_path / "plugins/demo").mkdir(parents=True)
    (tmp_path / "plugins/demo/plugin.yaml").write_text("id: demo\nname: Demo\nworkflows: [a, b]\n", encoding="utf-8")
    (tmp_path / "plugins/demo/plugin.py").write_text("x = 1\n" * 950, encoding="utf-8")
    assert status.version(tmp_path) == "1.2.3" and status.schema(tmp_path) == 2
    assert status.plugins(tmp_path) == [{"id": "demo", "name": "Demo", "runs_on": "any", "workflows": 2}]
    c = status.code(tmp_path)
    assert c["big"] == [(950, "plugins/demo/plugin.py")] and c["python"]["plugins"] == 950
