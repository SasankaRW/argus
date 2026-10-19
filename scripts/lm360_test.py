"""Test the DeepCool LM360 cooler screen: can Argus draw on it, and how fast?

Run on the PC (PowerShell), with DeepCool's own app closed:

    .\\.venv\\Scripts\\pip install -e .[eyes]
    .\\.venv\\Scripts\\python scripts\\lm360_test.py              # eyes test + frame-rate test
    .\\.venv\\Scripts\\python scripts\\lm360_test.py --gif x.gif  # also play a GIF on it for 20 s

Measured on 2026-10-19: 39.5 frames a second with Windows' own driver.
"""

from __future__ import annotations

import argparse
import sys
import time

from argus.eyes import Stopper
from argus.lm360 import H, Screen, W, to_rgb565


def eyes(open_amount: float):
    """Two white eyes on black; open_amount 1 = open, 0 = shut (a blink)."""
    from PIL import Image, ImageChops, ImageDraw, ImageFilter

    s = 2
    im = Image.new("L", (W * s, H * s), 0)
    d = ImageDraw.Draw(im)
    h = max(4, 92 * open_amount)
    for cx in (125, 275):
        d.rounded_rectangle([(cx - 46) * s, (150 - h / 2) * s, (cx + 46) * s, (150 + h / 2) * s],
                            radius=min(24 * s, h * s / 2), fill=255)
    im = im.resize((W, H), Image.LANCZOS)
    return ImageChops.add(im, im.filter(ImageFilter.GaussianBlur(10)))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", type=int, default=120, help="frames for the speed test")
    ap.add_argument("--gif", help="also play this GIF on the screen")
    ap.add_argument("--seconds", type=float, default=20, help="how long to play the GIF")
    args = ap.parse_args()

    stop = Stopper()  # Ctrl+C ends the test between frames: a frame cut in half leaves the screen black
    stop.install()
    screen = Screen()
    if not screen.open():
        sys.exit(screen.why)
    print("Connected to the LM360 screen.")

    screen.frame(to_rgb565(eyes(1.0)))
    print("Step 1: the screen should now show two white eyes.")
    time.sleep(3)

    cycle = [to_rgb565(eyes(o)) for o in (1, 0.8, 0.5, 0.2, 0.05, 0.2, 0.5, 0.8)]
    t0 = time.perf_counter()
    for i in range(args.frames):
        if stop():
            break
        screen.frame(cycle[i % len(cycle)])
    dt = time.perf_counter() - t0
    fps = args.frames / dt
    print(f"Step 2: {args.frames} frames in {dt:.2f} s = {fps:.1f} fps ({1000 / fps:.1f} ms per frame).")

    if args.gif and not stop():
        from PIL import Image, ImageSequence

        gif = Image.open(args.gif)
        frames = [(to_rgb565(f), f.info.get("duration", 50) / 1000) for f in ImageSequence.Iterator(gif)]
        print(f"Step 3: playing {args.gif} ({len(frames)} frames) for {args.seconds:.0f} s ...")
        end, nxt, i = time.perf_counter() + args.seconds, time.perf_counter(), 0
        while time.perf_counter() < end and not stop():
            data, dur = frames[i % len(frames)]
            screen.frame(data)
            nxt += dur
            time.sleep(max(0.0, nxt - time.perf_counter()))
            i += 1

    screen.frame(to_rgb565(eyes(1.0)))
    screen.close()
    print("Finished.")


if __name__ == "__main__":
    main()
