"""Which vision model finds the right button? (qwen2.5vl against the newer Qwen3-VL and GUI-Owl, on your screens)

    python -m argus.vision_test shots/ --capture 20            collect the test set from your own apps (below)
    python -m argus.vision_test shots/ --preset                qwen2.5vl:7b against qwen3-vl:8b (ollama pull both)
    python -m argus.vision_test shots/ --models qwen2.5vl:7b "hf.co/…/UI-TARS-1.5-7B-GGUF@thousand"

`--capture N`: every 8 seconds the window in front is saved with up to 5 of its named buttons, boxes and links as
cases (UI Automation knows exactly where they are), until N windows are kept: switch between your apps meanwhile
(Spotify, Settings, Notepad, Chrome, Explorer, Calculator, ...). Pictures stay in that folder.

`shots/cases.yaml` lists screenshots of real windows (Spotify, Chrome, Settings, …) and what to click:

    - {image: spotify-search.png, goal: "the search box", box: [412, 18, 760, 58]}
    - {image: settings.png, goal: "Bluetooth & devices", box: [10, 200, 300, 240]}

`box` is the right area in the picture's pixels (left, top, right, bottom; Paint shows them). Each model gets the
picture and the goal and answers where to click. A hit is a point inside the box. Printed: hits, misses and seconds
per model, so the better one becomes V1 (`models.tiers.V1` in argus.yaml).

A model name ending in "@thousand" answers in 0-1000 coordinates (UI-TARS does); others answer in pixels.
Nothing is sent anywhere but your Ollama; the pictures stay on this PC.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

PROMPT = ("You see a screenshot of one window ({w} x {h} pixels). Where should one click for: {goal}?\n"
          'Answer as JSON {{"x": <pixels from the left>, "y": <pixels from the top>}}.')
TARS = ("You are a GUI agent. You are given a task and a screenshot. Output the next action.\n"
        "Action space: click(start_box='(x1,y1)')\nTask: click {goal}")


PRESET = ["qwen2.5vl:7b", "qwen3-vl:8b@thousand"]  # Qwen3-VL (and GUI-Owl, built on it) answer in 0-1000
_CASE_ROLES = {"Button", "Edit", "Hyperlink", "MenuItem", "TabItem", "CheckBox", "ListItem", "ComboBox",
               "SplitButton", "RadioButton", "button", "textbox", "link", "menuitem"}


def cases_from(image: str, items: list[dict], boxes: dict[int, tuple[int, int, int, int]],
               rect: tuple[int, int, int, int], scale: float, k: int = 5) -> list[dict[str, Any]]:
    """Up to `k` test cases from one window: its named controls of clickable kinds, spread over the list, each with
    its box in the picture's pixels."""
    from .worker.marks import to_picture

    seen: set[str] = set()
    good = []
    for it in items:
        name = str(it.get("name") or "").strip()
        box = boxes.get(it["n"])
        if not name or len(name) > 40 or name.lower() in seen or box is None or it.get("role") not in _CASE_ROLES:
            continue
        b = to_picture(box, rect, scale)
        if b["w"] < 6 or b["h"] < 6 or b["x"] < 0 or b["y"] < 0:
            continue
        seen.add(name.lower())
        good.append({"image": image, "goal": f'the "{name}" {str(it["role"]).lower()}',
                     "box": [b["x"], b["y"], b["x"] + b["w"], b["y"] + b["h"]]})
    if len(good) <= k:
        return good
    step = len(good) / k
    return [good[int(i * step)] for i in range(k)]


def capture(folder: Path, count: int, every: float = 8.0, log=print) -> int:  # pragma: no cover - Windows only
    """Collect `count` windows (the one in front, every `every` seconds) as test cases in folder/cases.yaml."""
    import ctypes

    import yaml

    from .worker import winmsg
    from .worker.marks import shrink
    from .worker.uia import UiaSurface

    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "cases.yaml"
    cases = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else []
    kept, seen = 0, set()
    while kept < count:
        time.sleep(every)
        hwnd = int(ctypes.windll.user32.GetForegroundWindow() or 0)
        if not hwnd or hwnd in seen:
            continue
        try:
            uia = UiaSurface(hwnd)
            snap = uia.snapshot()
            png, rect = winmsg.capture(hwnd)
            png, scale = shrink(png, 1280)
        except Exception as e:  # noqa: BLE001 - a window that went away
            log(f"skipped a window: {e}")
            continue
        name = f"shot-{time.strftime('%Y%m%d-%H%M%S')}.png"
        got = cases_from(name, snap["items"], uia.boxes, rect, scale)
        if len(got) < 2:
            log(f"skipped {snap.get('title')!r}: too few named controls")
            continue
        (folder / name).write_bytes(png)
        cases += got
        path.write_text(yaml.safe_dump(cases, sort_keys=False, allow_unicode=True), encoding="utf-8")
        seen.add(hwnd)
        kept += 1
        log(f"kept {kept}/{count}: {snap.get('title')!r} ({len(got)} cases)")
    return kept


def point_of(text: str, w: int, h: int, thousand: bool) -> tuple[float, float] | None:
    """The point a model answered: JSON {x, y}, or the first "(x, y)" (UI-TARS); scaled when in 0-1000."""
    x = y = None
    try:
        d = json.loads(re.search(r"\{.*\}", text, re.S).group(0))  # type: ignore[union-attr]
        x, y = float(d["x"]), float(d["y"])
    except (AttributeError, ValueError, KeyError, TypeError):
        m = re.search(r"\(\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*\)", text)
        if m:
            x, y = float(m.group(1)), float(m.group(2))
    if x is None or y is None:
        return None
    return (x * w / 1000, y * h / 1000) if thousand else (x, y)


def hit(p: tuple[float, float] | None, box: list[float]) -> bool:
    return p is not None and box[0] <= p[0] <= box[2] and box[1] <= p[1] <= box[3]


def ask(url: str, model: str, png: bytes, goal: str, w: int, h: int, thousand: bool, timeout: float = 120) -> str:
    import urllib.request

    prompt = (TARS if thousand else PROMPT).format(goal=goal, w=w, h=h)
    body = {"model": model, "stream": False, "options": {"temperature": 0},
            "messages": [{"role": "user", "content": prompt, "images": [base64.b64encode(png).decode()]}]}
    req = urllib.request.Request(f"{url.rstrip('/')}/api/chat", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - your own Ollama
        return str(json.loads(r.read())["message"]["content"])


def run(folder: Path, models: list[str], url: str, asker: Any = ask, log=print) -> dict[str, Any]:
    import yaml
    from PIL import Image

    cases = yaml.safe_load((folder / "cases.yaml").read_text(encoding="utf-8")) or []
    out: dict[str, Any] = {}
    for spec in models:
        model, thousand = spec.removesuffix("@thousand"), spec.endswith("@thousand")
        rows = []
        for c in cases:
            png = (folder / c["image"]).read_bytes()
            w, h = Image.open(folder / c["image"]).size
            t0 = time.perf_counter()
            try:
                said = asker(url, model, png, c["goal"], w, h, thousand)
            except Exception as e:  # noqa: BLE001 - a model that isn't there: every case a miss
                said = f"error: {e}"
            took = time.perf_counter() - t0
            p = point_of(said, w, h, thousand)
            ok = hit(p, c["box"])
            rows.append({"image": c["image"], "goal": c["goal"], "point": p, "hit": ok, "seconds": round(took, 2)})
            log(f"{'hit ' if ok else 'MISS'} {took:5.1f}s  {model[:40]:40} {c['goal'][:40]!r} -> {p}")
        n = len(rows)
        out[spec] = {"hits": sum(r["hit"] for r in rows), "of": n,
                     "avg_s": round(sum(r["seconds"] for r in rows) / max(1, n), 2), "cases": rows}
    log("")
    ranked = sorted(out.items(), key=lambda kv: (-kv[1]["hits"], kv[1]["avg_s"]))
    for spec, r in ranked:
        log(f"{spec}: {r['hits']}/{r['of']} hits, {r['avg_s']} s each")
    if ranked:
        best = ranked[0][0]
        log(f"\nBest: {best}. To use it: models.tiers.V1.model: {best.removesuffix('@thousand')!r} in argus.yaml"
            + (", and the Workstation's setting vision_coords: thousand" if best.endswith("@thousand") else ""))
    return out


def main(argv: list[str] | None = None) -> int:
    from .config import load_config

    ap = argparse.ArgumentParser(prog="vision-test", description=__doc__.split("\n")[0])
    ap.add_argument("folder", type=Path)
    ap.add_argument("--models", nargs="+", default=None,
                    help='Ollama model names ("…@thousand": answers in 0-1000, as Qwen3-VL, GUI-Owl, UI-TARS)')
    ap.add_argument("--preset", action="store_true", help=f"the usual contest: {', '.join(PRESET)}")
    ap.add_argument("--capture", type=int, default=0, metavar="N", help="collect N windows as test cases first")
    a = ap.parse_args(argv)
    cfg = load_config()
    if a.capture:
        print(f"Switch between your apps: the window in front is kept every 8 s ({a.capture} windows).")
        capture(a.folder, a.capture)
        if not a.preset and not a.models:
            return 0
    models = PRESET if a.preset else a.models
    if not models:
        v1 = cfg.models.tiers.get("V1")
        models = [v1.model] if v1 and v1.model else ["qwen2.5vl:7b"]
    if not (a.folder / "cases.yaml").exists():
        print(f"no {a.folder / 'cases.yaml'}: see python -m argus.vision_test --help", file=sys.stderr)
        return 2
    res = run(a.folder, models, cfg.ollama.url)
    keep = cfg.db_path.parent / "vision-test"
    keep.mkdir(parents=True, exist_ok=True)
    (keep / f"{time.strftime('%Y%m%d-%H%M%S')}.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
