"""Reading the logs of everything `dev.ps1 up` starts, for Helios's Logs page and `dev.ps1 logs`.

Sources are the `*.log` files next to argusd's own log (argus.log, worker.log, ollama.log, ...), plus the Ollama
desktop app's server.log when Ollama was started by the app rather than by `up`. Lines are Argus's JSON lines,
Ollama's `key=value` lines, or plain text (a crash traceback); all come back as one shape:
{ts, level, logger, msg, extra}.

    python -m argus.logview                 follow every source in one terminal, coloured
    python -m argus.logview worker argus    only these
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
KV = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|\S+)')
LEVEL_WORD = re.compile(r"\b(DEBUG|INFO|WARN|WARNING|ERROR|CRITICAL|FATAL|Traceback)\b")
MAX_READ = 2 * 1024 * 1024  # never read more than this per request


def sources(log_dir: Path) -> dict[str, Path]:
    """name -> file. Rotated copies (argus.log.1) are left out."""
    out: dict[str, Path] = {}
    if log_dir.is_dir():
        for p in sorted(log_dir.glob("*.log")):
            # "<name>-crash.log" holds what a process printed before it could log (a traceback): only when not empty
            if NAME.match(p.stem) and not (p.stem.endswith("-crash") and p.stat().st_size == 0):
                out[p.stem] = p
    app = Path(os.environ.get("LOCALAPPDATA", "~/AppData/Local")).expanduser() / "Ollama" / "server.log"
    if "ollama" not in out and app.is_file():
        out["ollama"] = app
    return out


def parse(line: str, source: str) -> dict[str, Any] | None:
    line = line.rstrip("\r\n")
    if not line.strip():
        return None
    if line.startswith("{"):
        try:
            d = json.loads(line)
            if isinstance(d, dict) and "msg" in d:
                extra = {k: v for k, v in d.items() if k not in ("ts", "level", "logger", "msg")}
                return {"source": source, "ts": d.get("ts"), "level": str(d.get("level", "info")).lower(),
                        "logger": d.get("logger"), "msg": str(d["msg"]), "extra": extra}
        except ValueError:
            pass
    if line.startswith("time=") and " msg=" in line:  # Ollama (Go slog)
        kv = {k: v[1:-1].replace('\\"', '"') if v.startswith('"') else v for k, v in KV.findall(line)}
        extra = {k: v for k, v in kv.items() if k not in ("time", "level", "msg", "source")}
        return {"source": source, "ts": kv.get("time"), "level": kv.get("level", "info").lower(),
                "logger": kv.get("source"), "msg": kv.get("msg", ""), "extra": extra}
    m = LEVEL_WORD.search(line[:120])
    level = (m.group(1).lower() if m else "info").replace("warning", "warn").replace("traceback", "error")
    return {"source": source, "ts": None, "level": level, "logger": None, "msg": line, "extra": {}}


def read(path: Path, source: str, *, after: int | None = None, lines: int = 300) -> dict[str, Any]:
    """New entries since byte `after` (or the last `lines` entries). Returns {entries, offset}; pass `offset`
    back as `after` next time. A file that shrank (rotated) is read from the start."""
    size = path.stat().st_size
    if after is not None and after <= size:
        start = max(after, size - MAX_READ)
    else:
        start = max(0, size - min(MAX_READ, 200 * max(lines, 1)))
    with open(path, "rb") as f:
        f.seek(start)
        data = f.read(size - start)
    text = data.decode("utf-8", errors="replace")
    if start > 0 and (after is None or start != after):
        text = text.split("\n", 1)[1] if "\n" in text else ""  # drop a partial first line
    end = start + len(data)
    if not text.endswith("\n") and "\n" in text:  # keep a half-written last line for the next read
        cut = text.rfind("\n") + 1
        end -= len(text[cut:].encode("utf-8"))
        text = text[:cut]
    elif not text.endswith("\n"):
        end, text = start, ""
    entries = [e for e in (parse(ln, source) for ln in text.split("\n")) if e]
    if after is None:
        entries = entries[-lines:]
    return {"entries": entries, "offset": end}


# ------------------------------------------------------------------ terminal view

COLOURS = {"debug": "90", "info": "37", "warn": "33", "warning": "33", "error": "31", "critical": "31"}
SOURCE_COLOURS = ["36", "35", "34", "32", "96", "95"]


def _fmt(e: dict[str, Any], width: int, colour: str) -> str:
    ts = (e["ts"] or "")[11:19] if e["ts"] else "        "
    lvl = e["level"][:4].upper().ljust(4)
    extra = " ".join(f"{k}={v}" for k, v in list(e["extra"].items())[:6] if k != "exc")
    msg = e["msg"] + (f"  \x1b[90m{extra}\x1b[0m" if extra else "")
    exc = f"\n\x1b[31m{e['extra']['exc']}\x1b[0m" if e["extra"].get("exc") else ""
    return (f"\x1b[90m{ts}\x1b[0m \x1b[{colour}m{e['source'].ljust(width)}\x1b[0m "
            f"\x1b[{COLOURS.get(e['level'], '37')}m{lvl}\x1b[0m {msg}{exc}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="argus-logs", description="Follow Argus logs in one terminal")
    p.add_argument("names", nargs="*", help="sources to show (default: all)")
    p.add_argument("--dir", default="logs")
    p.add_argument("--lines", type=int, default=40, help="lines of history per source first")
    p.add_argument("--level", default="debug", choices=["debug", "info", "warn", "error"])
    args = p.parse_args(argv)
    if os.name == "nt":
        os.system("")  # turn on ANSI colours in the Windows console
    order = ["debug", "info", "warn", "error"]
    floor = order.index(args.level)
    pos: dict[str, int | None] = {}
    print("\x1b[90mFollowing Argus logs (Ctrl+C to stop). Helios shows the same on its Logs page.\x1b[0m")
    try:
        while True:
            srcs = {n: f for n, f in sources(Path(args.dir)).items() if not args.names or n in args.names}
            width = max((len(n) for n in srcs), default=6)
            for i, (n, f) in enumerate(srcs.items()):
                try:
                    r = read(f, n, after=pos.get(n), lines=args.lines)
                except OSError:
                    continue
                pos[n] = r["offset"]
                for e in r["entries"]:
                    lv = "warn" if e["level"] == "warning" else e["level"]
                    rank = order.index(lv) if lv in order else 3
                    if rank >= floor:
                        print(_fmt(e, width, SOURCE_COLOURS[i % len(SOURCE_COLOURS)]), flush=True)
            time.sleep(1)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
