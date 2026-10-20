"""The live view's feeds: the PC listener's voice levels and the model answering now (memory only, on argusd)."""

from __future__ import annotations

from pathlib import Path

from argus.config import load_config
from argus.context import Argus
from test_worker import Server, client


def make(tmp_path: Path) -> Argus:
    (tmp_path / "argus.yaml").write_text("logging:\n  file: null\n", encoding="utf-8")
    return Argus(load_config(tmp_path / "argus.yaml"))


def test_live_levels_from_the_listener():
    from argus.ari_listen import LiveLevels, level

    assert level(1e-9) == 0.0 and level(1.0) == 1.0 and 0 < level(0.01) < 1
    t, busy = [0.0], [False]
    lv = LiveLevels(None, active=lambda: busy[0], speaking=lambda: False, out_levels=lambda: [],
                    english=lambda: 0.9, clock=lambda: t[0])
    for x in (0.001, 0.05, 0.002):
        lv.add(x)
    t[0] = 1.5
    b = lv.body()
    assert len(b["mic"]) == 1 and b["english"] == 0.9  # quiet: one bar a second
    busy[0] = True
    for x in (0.001, 0.05):
        lv.add(x)
    t[0] = 1.6
    assert lv.body() is None  # too soon
    t[0] = 1.8
    assert len(lv.body()["mic"]) == 2


def test_live_on_argusd(tmp_path):
    with Server(make(tmp_path).open()) as srv:
        cl = client(srv.url)
        cl.post("/ari/live", {"mic": [0.2, 0.4, 7], "speaking": True, "english": 0.8})
        cl.post("/models/live", {"state": "text", "job_id": "j", "model": "qwen3:8b", "text": "{\"a\""})
        live = cl.get("/live")
        assert live["voice"]["mic"] == [0.2, 0.4, 1.0] and live["voice"]["speaking"] is True
        assert live["model"]["text"] == "{\"a\"" and live["model_at"] > 0
        cl.post("/models/live", {"state": "end", "job_id": "j", "verdict": "ok"})
        cl.post("/models/live", {"state": "text", "job_id": "j", "text": "late"})
        assert cl.get("/live")["model"]["state"] == "end"  # a late piece doesn't undo the end
