"""Numbered marks on a window's picture (the "Set-of-Mark" technique, P7 part 4).

The vision model gets the window with a numbered box drawn on every control Ari knows about (from UI Automation)
and answers with a number ("click 7") instead of guessing a position. That is more exact than a raw (x, y); a
position is only for something that has no box.

The picture lives in memory for one step: never saved, never sent to Claude.
"""

from __future__ import annotations

import io

COLOURS = [(255, 45, 85), (0, 122, 255), (52, 199, 89), (255, 149, 0), (175, 82, 222), (255, 204, 0)]


def draw_marks(png: bytes, boxes: list[dict]) -> bytes:
    """`boxes`: [{n, x, y, w, h}] in the picture's pixels. Returns a PNG with each box outlined and its number in a
    filled tag at the top left (inside the picture)."""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.open(io.BytesIO(png)).convert("RGB")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default(size=14)
    except TypeError:  # an older Pillow
        font = ImageFont.load_default()
    W, H = img.size
    for b in boxes:
        x, y, w, h = int(b["x"]), int(b["y"]), int(b["w"]), int(b["h"])
        if w < 3 or h < 3 or x >= W or y >= H or x + w <= 0 or y + h <= 0:
            continue
        c = COLOURS[int(b["n"]) % len(COLOURS)]
        d.rectangle([x, y, x + w - 1, y + h - 1], outline=c, width=2)
        label = str(b["n"])
        tw = int(d.textlength(label, font=font)) + 6
        tx, ty = max(0, min(x, W - tw)), max(0, y - 16 if y >= 16 else y)
        d.rectangle([tx, ty, tx + tw, ty + 15], fill=c)
        d.text((tx + 3, ty + 1), label, fill=(255, 255, 255), font=font)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def to_picture(box: tuple[int, int, int, int], window: tuple[int, int, int, int], scale: float = 1.0) -> dict:
    """A control's screen rectangle (left, top, right, bottom) -> {x, y, w, h} in the window picture's pixels."""
    left, top, right, bottom = box
    return {"x": round((left - window[0]) * scale), "y": round((top - window[1]) * scale),
            "w": round((right - left) * scale), "h": round((bottom - top) * scale)}


def to_screen(x: float, y: float, window: tuple[int, int, int, int], scale: float = 1.0) -> tuple[int, int]:
    """A point in the window picture -> the screen (for a click by window message)."""
    return round(window[0] + x / scale), round(window[1] + y / scale)


def shrink(png: bytes, max_side: int = 1280) -> tuple[bytes, float]:
    """A smaller picture for the model, and the scale used (picture pixels per screen pixel)."""
    from PIL import Image

    img = Image.open(io.BytesIO(png))
    s = min(1.0, max_side / max(img.size))
    if s >= 1.0:
        return png, 1.0
    img = img.resize((round(img.size[0] * s), round(img.size[1] * s)))
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue(), s
