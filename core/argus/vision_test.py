"""Which vision model finds the right button? (P7 part 4: UI-TARS-1.5-7B against qwen2.5vl, on your screens)

    python -m argus.vision_test shots/                         every model in --models on shots/cases.yaml
    python -m argus.vision_test shots/ --models qwen2.5vl:7b "hf.co/…/UI-TARS-1.5-7B-GGUF@thousand"

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
    for spec, r in sorted(out.items(), key=lambda kv: (-kv[1]["hits"], kv[1]["avg_s"])):
        log(f"{spec}: {r['hits']}/{r['of']} hits, {r['avg_s']} s each")
    return out


def main(argv: list[str] | None = None) -> int:
    from .config import load_config

    ap = argparse.ArgumentParser(prog="vision-test", description=__doc__.split("\n")[0])
    ap.add_argument("folder", type=Path)
    ap.add_argument("--models", nargs="+", default=None, help='Ollama model names ("…@thousand" for UI-TARS)')
    a = ap.parse_args(argv)
    cfg = load_config()
    models = a.models
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
