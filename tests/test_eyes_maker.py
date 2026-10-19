"""The eyes clip maker (plugins/eyes): which apps earn a clip, Claude's result, the checks a clip must pass, and
a whole run with a fake Claude (the real studio renders the clip)."""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import pytest

from argus.eyes import Clip, check_clip, pack
from argus.lm360 import FRAME_BYTES
from argus.worker import PermanentError

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("eyes_maker", ROOT / "plugins" / "eyes" / "plugin.py")
mk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mk)
STUDIO = ROOT / "plugins" / "eyes" / "studio"


def px(v: int) -> bytes:  # a flat grey frame (RGB565 of grey v)
    w = ((v >> 3) << 11) | ((v >> 2) << 5) | (v >> 3)
    return w.to_bytes(2, "little") * (FRAME_BYTES // 2)


def test_which_apps_have_earned_a_clip(tmp_path):
    (tmp_path / "code.eyes").write_bytes(b"x")
    seen = {"code": {"seconds": 900}, "spotify": {"seconds": 300}, "youtube": {"seconds": 600},
            "notepad": {"seconds": 30}, "steam": {"seconds": 500}, "discord": {"seconds": 400},
            "default": {"seconds": 999}, "../x": {"seconds": 999}}
    got = mk.candidates(seen, tmp_path, {"steam": "default"}, {"discord": 2}, 120)
    assert got == ["youtube", "spotify"]  # most time first; has a clip / mapped / tried twice / too new: skipped


def test_claudes_result_is_checked():
    names = {"code", "browser", "default"}
    assert mk.parse_result('{"reuse": "code"}', "cursor", names, own=False) == {"reuse": "code"}
    assert mk.parse_result('{"clip": "spotify.py", "about": "music:  bliss\\n"}', "spotify", names, False) == \
        {"clip": "spotify.py", "about": "music: bliss"}
    for bad, own in [('{"reuse": "code"}', True),           # youtube, github, gmail: always their own
                     ('{"reuse": "default"}', False), ('{"reuse": "nope"}', False),
                     ('{"clip": "../x.py"}', False), ('{"clip": "render.py"}', False), ("no", False)]:
        with pytest.raises(PermanentError):
            mk.parse_result(bad, "youtube", names, own)


def test_the_prompt_offers_reuse_only_when_allowed():
    clips = [("code", "coding: reads, focuses")]
    assert "reuse" in mk.prompt("cursor", False, clips) and "code: coding" in mk.prompt("cursor", False, clips)
    assert "reuse" not in mk.prompt("youtube", True, clips)
    assert "make it dance" in mk.prompt("spotify", True, clips, note="make it dance")
    assert "problems" in mk.prompt("spotify", True, clips, problems=["the loop jumps"])


def test_the_checks_a_clip_must_pass():
    rest = px(0)
    home, away = px(0), px(60)
    good = pack([home] * 20 + [away] * 200 + [home] * 20 + [away] * 200 + [home] * 20, neutral=[0])
    chk = check_clip(good, rest)
    assert chk["problems"] == [] and chk["seconds"] == 23.0 and chk["neutral"][:2] == [0, 1]
    long_away = pack([home] * 20 + [away] * 400 + [home] * 20)
    assert any("without a home beat" in p for p in check_clip(long_away, rest)["problems"])
    jumps = pack([home] * 20 + [away] * 400 + [px(200)] * 20)
    probs = check_clip(jumps, rest)["problems"]
    assert any("jumps" in p for p in probs)
    short = pack([home] * 100)
    assert any("s long" in p for p in check_clip(short, rest)["problems"])
    wrong_start = pack([away] * 20 + [home] * 400 + [away] * 20)
    assert any("resting pose" in p for p in check_clip(wrong_start, rest)["problems"])
    red = (0xF800).to_bytes(2, "little") * (FRAME_BYTES // 2)
    assert any("colours" in p for p in check_clip(pack([home] * 20 + [red] * 400 + [home] * 20), rest)["problems"])
    bright = px(255)
    assert any("lit" in p for p in check_clip(pack([home] * 20 + [bright] * 400 + [home] * 20), rest)["problems"])
    assert check_clip(b"junk", rest)["problems"]


class Store(dict):
    def set(self, k, v):
        self[k] = v


class Ctx:
    def __init__(self, **config):
        self.config = {"model": "sonnet", "minutes": 1, "own_clip": ["youtube", "github", "gmail"], **config}
        self.store, self.logs, self.dry_run = Store(), [], False

    def log(self, msg, **kv):
        self.logs.append((msg, kv))


@pytest.fixture
def clips(tmp_path):
    d = tmp_path / "eyes" / "clips"
    d.mkdir(parents=True)
    rest = ROOT / "data" / "eyes" / "clips" / "default.eyes"
    if not rest.exists():
        pytest.skip("needs data/eyes/clips/default.eyes (the resting pose) on this PC")
    shutil.copy2(rest, d / "default.eyes")
    return d


SHORT = '''from clipkit import make
ABOUT = "test: looks left, smiles"
BEATS = [("home", "neutral", 1.8, {}), ("left", "curious", 3.0, {"track": "farL"}), ("home", "neutral", 1.8, {}),
         ("smile", "happy", 3.0, {}), ("home", "neutral", 1.8, {}), ("look", "notice", 3.0, {"track": "up"}),
         ("home", "neutral", 1.8, {}), ("ok", "content", 3.0, {}), ("home", "neutral", 1.8, {})]
STORY = make(BEATS)
'''


def test_a_whole_run_with_a_fake_claude(clips, monkeypatch):
    prompts = []

    def fake_claude(command, model, text, cwd, timeout):
        prompts.append(text)
        assert (cwd / "STYLE.md").exists() and (cwd / "examples" / "default.py").exists()
        assert "default: calm default" in (cwd / "clips.md").read_text()
        (cwd / "spotify.py").write_text(SHORT)
        (cwd / "clipkit.py").write_text("broken = True")  # Argus puts the kit back before it renders
        (cwd / "result.json").write_text(json.dumps({"clip": "spotify.py", "about": "music: sways, bliss"}))
        return "{}"

    monkeypatch.setattr(mk, "run_claude", fake_claude)
    ctx = Ctx()
    out = mk.run_one(ctx, clips, "spotify")
    assert out.get("clip") == "spotify.eyes" and out["attempts"] == 1, out
    c = Clip.load(clips / "spotify.eyes")
    assert c.about == "music: sways, bliss" and len(c.neutral) > 50 and 400 < len(c) < 440
    assert ctx.store["runs"]["n"] == 1 and ctx.store["busy"] == {} and ctx.store["history"][-1]["key"] == "spotify"
    assert "reuse" in prompts[0]


def test_reuse_maps_the_key_and_a_bad_clip_gets_one_more_try(clips, monkeypatch):
    def reuse(command, model, text, cwd, timeout):
        (cwd / "result.json").write_text('{"reuse": "default"}')
        return ""

    monkeypatch.setattr(mk, "run_claude", reuse)
    out = mk.run_one(Ctx(), clips, "cursor")
    assert "failed" in out  # default is never reused
    shutil.copy2(clips / "default.eyes", clips / "code.eyes")
    monkeypatch.setattr(mk, "run_claude",
                        lambda c, m, t, cwd, to: str((cwd / "result.json").write_text('{"reuse": "code"}')))
    out = mk.run_one(Ctx(), clips, "cursor")
    assert out["reused"] == "code" and json.loads((clips / "map.json").read_text()) == {"cursor": "code"}

    calls = []

    def bad_then_good(command, model, text, cwd, timeout):
        calls.append(text)
        beats = SHORT  # the first one "fails" the (patched) checks, the second passes
        (cwd / "steam.py").write_text(beats)
        (cwd / "result.json").write_text('{"clip": "steam.py", "about": "games"}')

    def check(data, rest):
        got = check_clip(data, rest)
        return {**got, "problems": ["the loop jumps (test)"]} if len(calls) == 1 else got

    monkeypatch.setattr(mk, "run_claude", bad_then_good)
    import argus.eyes as ae
    monkeypatch.setattr(ae, "check_clip", check)
    out = mk.run_one(Ctx(), clips, "steam")
    assert out.get("attempts") == 2 and "the loop jumps (test)" in calls[1]
