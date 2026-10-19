"""Look at a clip without rendering all of it:  python sheet.py myclip.py [--at 12.5 13 13.5]

Writes sheet.png: a picture every 1.5 s, each labelled with its time and beat, plus the list of beats.
With --at, writes zoom.png instead: those moments at full size (to check a blink or a fast change).
Read the PNG to see it."""
import argparse
import importlib.util
import sys
from pathlib import Path

import eyecore as C
from PIL import Image, ImageDraw


def load(path):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    spec = importlib.util.spec_from_file_location("clip", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def beat_at(story, t):
    i = story.entry_at(t)
    return story.TAGS[i], story.SEQ[i][0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("clip")
    ap.add_argument("--at", type=float, nargs="*", help="seconds to show at full size")
    ap.add_argument("--every", type=float, default=1.5)
    a = ap.parse_args()
    story = load(a.clip).STORY
    traj, wtraj = story.simulate()
    print(f"{story.TOTAL:.1f} s, {len(story.SEQ)} beats, {len(story.BLINKS)} blinks")
    for tag, start, (shape, secs, _) in zip(story.TAGS, story.STARTS, story.SEQ, strict=True):
        print(f"  {start:5.1f}s  {secs:4.1f}s  {shape:<10} {tag}")
    if a.at:
        times = [t for t in a.at if 0 <= t < story.TOTAL][:8]
        w, h, cols = 320, 240, 2
        out, name = Image.new("RGB", (cols * w, ((len(times) + cols - 1) // cols) * h)), "zoom.png"
    else:
        times = [i * a.every for i in range(int(story.TOTAL / a.every))]
        w, h, cols = 160, 120, 6
        out, name = Image.new("RGB", (cols * w, ((len(times) + cols - 1) // cols) * h)), "sheet.png"
    for k, t in enumerate(times):
        f = min(story.NF - 1, int(round(t * C.FPS)))
        im = story.frame(f, traj, wtraj, 0.8).convert("RGB").resize((w, h))
        tag, shape = beat_at(story, t)
        ImageDraw.Draw(im).text((3, 2), f"{t:.1f}s {tag}", fill=(255, 60, 60))
        out.paste(im, ((k % cols) * w, (k // cols) * h))
    out.save(name)
    print("wrote", name)


if __name__ == "__main__":
    main()
