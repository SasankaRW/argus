"""What the island reads from the PC: helpers that run everywhere (the Windows parts are tried on the PC)."""

from __future__ import annotations

import subprocess

from argus.island_live import (
    Hardware,
    app_name,
    clock,
    level,
    month_grid,
    others,
    parse_gpu,
    parse_hotkey,
    position_now,
    rate,
    weekday_letters,
)


def test_a_month_fits_its_rows():
    feb = month_grid(2026, 2)  # 1 Feb 2026 is a Sunday: 5 rows starting Monday
    assert len(feb) == 5 and feb[0][:6] == [0] * 6 and feb[0][6] == 1 and max(feb[-1]) == 28
    assert len(month_grid(2026, 2, 6)) == 4  # weeks from Sunday: exactly 4
    assert weekday_letters(6)[0] == "S" and weekday_letters(0)[0] == "M"


def test_load_bars_and_speeds_in_words():
    assert [level(x) for x in (None, 40, 76, 91)] == ["ok", "ok", "warm", "hot"]
    assert rate(0) == "0 B/s" and rate(840_000) == "840 KB/s" and rate(12_400_000) == "12.4 MB/s"
    assert clock(187) == "3:07" and clock(3765) == "1:02:45" and clock(None) == "–:––"


def test_who_has_the_mic_without_argus_itself():
    assert app_name(r"C:#Program Files#Discord#Discord.exe") == "Discord"
    assert app_name("Microsoft.WindowsCamera_8wekyb3d8bbwe") == "WindowsCamera"
    entries = [r"C:#Users#sas#argus#.venv#Scripts#python.exe", r"C:#Apps#Zoom#Zoom.exe", r"C:#Apps#Zoom#Zoom.exe"]
    assert others(entries, [r"C:\Users\sas\argus\.venv"]) == ["Zoom"]


def test_the_song_moves_between_reports():
    assert position_now(10.0, 200.0, 100.0, True, now=104.0) == 14.0
    assert position_now(10.0, 200.0, 100.0, False, now=104.0) == 10.0  # paused: stays
    assert position_now(198.0, 200.0, 100.0, True, now=150.0) == 200.0  # never past the end
    assert position_now(None, 200.0, 100.0, True) is None


def test_global_keys():
    assert parse_hotkey("ctrl+alt+space") == (0x3, 0x20)
    assert parse_hotkey("Ctrl + Shift + A") == (0x6, ord("A"))
    assert parse_hotkey("win+f9") == (0x8, 0x78)
    assert parse_hotkey("space") is None and parse_hotkey("ctrl+nope") is None and parse_hotkey("") is None


def test_the_gpu_line_and_a_missing_nvidia_smi():
    g = parse_gpu("41, 7577, 12227, 58")
    assert g["util"] == 41 and round(g["mem_used"], 1) == 7.4 and round(g["mem"]) == 62 and g["temp"] == 58
    assert parse_gpu("garbage") is None

    def run(*a, **k):
        return subprocess.CompletedProcess(a, 0, stdout="12, 1024, 12288, [N/A]\n")

    hw = Hardware(run=run)
    hw._smi = "nvidia-smi"
    assert hw.read()["gpu"]["temp"] is None
    hw._smi = None
    assert "gpu" not in Hardware(run=run).read() or Hardware(run=run)._smi  # no nvidia-smi: no GPU row
