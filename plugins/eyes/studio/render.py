"""Render a clip for the cooler and check it:  python render.py myclip.py

Writes out/<name>.eyes (320 x 240, 20 fps, the player's format) and prints the checks Argus runs before it
saves a clip. Fix what it lists and render again."""
import argparse
import importlib.util
import sys
from pathlib import Path

import eyecore as C  # noqa: F401 - clipkit's engine

from argus.eyes import check_clip, pack
from argus.lm360 import to_rgb565

HERE = Path(__file__).resolve().parent


def load(path):
    sys.path.insert(0, str(HERE))
    spec = importlib.util.spec_from_file_location("clip", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def render(path, rest=None):
    """(clip bytes, the check) for a clip file."""
    mod = load(path)
    story = mod.STORY
    traj, wtraj = story.simulate()
    frames = [to_rgb565(story.frame(f, traj, wtraj, 0.8).convert("RGB")) for f in range(story.NF)]
    data = pack(frames, fps=C.FPS, about=str(getattr(mod, "ABOUT", "")))
    return data, check_clip(data, rest)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("clip")
    ap.add_argument("--rest", default="rest.frame", help="the shared resting pose (written by Argus)")
    a = ap.parse_args()
    rest_path = HERE / a.rest
    rest = rest_path.read_bytes() if rest_path.exists() else None
    data, chk = render(a.clip, rest)
    out = HERE / "out" / (Path(a.clip).stem + ".eyes")
    out.parent.mkdir(exist_ok=True)
    out.write_bytes(data)
    print(f"{out.name}: {chk['seconds']} s, {len(data) / 1e6:.1f} MB, seam {chk['seam']}, "
          f"{len(chk['neutral'])} home frames")
    for p in chk["problems"]:
        print("PROBLEM:", p)
    print("all checks passed" if not chk["problems"] else "fix the problems above and render again")
    return 1 if chk["problems"] else 0


if __name__ == "__main__":
    sys.exit(main())
