"""The pre-move check: the laptop, the token, this PC's Argus and Ollama, Fast Startup, the wired card."""

from __future__ import annotations

from argus import __version__
from argus.movecheck import FIX, LOOK, OK, check_files, check_here, check_laptop, check_windows


def fake(routes):
    return lambda url, token=None: routes.get((url, token), routes.get(url, (0, None)))


def marks(lines):
    return [(x.what, x.mark) for x in lines]


def test_the_laptop_and_the_token():
    get = fake({"http://lap:8600/version": (200, {"version": __version__}),
                ("http://lap:8600/workers", "good"): (200, []), ("http://lap:8600/workers", "bad"): (401, None)})
    assert marks(check_laptop("http://lap:8600", "good", get)) == [("laptop", OK), ("worker token", OK)]
    assert marks(check_laptop("http://lap:8600/", "bad", get))[1] == ("worker token", FIX)
    assert marks(check_laptop("http://lap:8600", None, get))[1] == ("worker token", FIX)
    assert marks(check_laptop("http://nowhere:8600", "good", get)) == [("laptop", FIX)]
    old = fake({"http://lap:8600/version": (200, {"version": "0.0.1"}), "http://lap:8600/workers": (200, [])})
    assert ("versions", LOOK) in marks(check_laptop("http://lap:8600", "t", old))


def test_this_pc_argus_must_be_stopped_and_ollama_have_the_models():
    tags = (200, {"models": [{"name": "qwen2.5-coder:7b"}, {"name": "llama3.1:8b"}]})
    get = fake({"http://127.0.0.1:11434/api/tags": tags})
    assert marks(check_here(get, ["qwen2.5-coder:7b"], "http://127.0.0.1:11434")) == [
        ("Argus on this PC", OK), ("Ollama models", OK)]
    lines = check_here(get, ["qwen2.5:14b"], "http://127.0.0.1:11434")
    assert lines[1].mark == FIX and "qwen2.5:14b" in lines[1].detail
    running = fake({"http://127.0.0.1:8600/version": (200, {}), "http://127.0.0.1:11434/api/tags": tags})
    assert check_here(running, [], "http://127.0.0.1:11434")[0].mark == FIX
    assert check_here(fake({}), [], "http://127.0.0.1:11434")[1].mark == FIX


def test_windows_fast_startup_and_the_wired_card():
    out = {"reg": "    HiberbootEnabled    REG_DWORD    0x1\n",
           "getmac": '"Ethernet","Realtek PCIe GbE","2C-F0-5D-11-22-33","\\Device\\Tcpip_{X}"\n'
                     '"Wi-Fi","Intel Wi-Fi 6","A0-B1-C2-D3-E4-F5","Media disconnected"\n'}
    lines = check_windows(lambda argv: out[argv[0]])
    assert lines[0].mark == FIX
    assert lines[1].mark == OK and "2C-F0-5D-11-22-33" in lines[1].detail and "A0-B1" not in lines[1].detail
    out["reg"], out["getmac"] = "HiberbootEnabled REG_DWORD 0x0", ""
    assert marks(check_windows(lambda argv: out[argv[0]])) == [("Fast Startup", OK), ("wired card", LOOK)]


def test_the_database_to_copy(tmp_path):
    assert check_files(tmp_path)[0].mark == LOOK
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "argus.db").write_bytes(b"x" * 2_000_000)
    assert "2.0 MB" in check_files(tmp_path)[0].detail
