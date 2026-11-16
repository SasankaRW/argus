"""Cut the outlined Material Symbols Rounded glyphs the send menu needs into glyphs.txt (committed, Apache 2.0 source).

    pip install fonttools brotli
    python windows/send-menu/make_glyphs.py [font.woff2]   (after `npm ci` in helios/, for the material-symbols package)

One line per glyph and weight (`name` = 400, `name-w500`): `name|M x y L x y C x1 y1 x2 y2 x y Z ...` on a 960 box, y down, weight 400, outlined (FILL 0), optical size
24 (the house icon recipe's glyph box). make-assets.ps1 draws them at build time, so no font or Python is needed there.
"""

from __future__ import annotations

import sys
from pathlib import Path

from fontTools.pens.basePen import BasePen
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

HERE = Path(__file__).resolve().parent
FONT = HERE.parents[1] / "helios" / "node_modules" / "material-symbols" / "material-symbols-rounded.woff2"
NAMES = ["smartphone", "laptop_chromebook", "computer"]   # phone, Calypso, PC


class PathPen(BasePen):
    def __init__(self, glyphs, scale):
        super().__init__(glyphs)
        self.out: list[str] = []
        self.k = scale

    def pt(self, p):
        return f"{p[0] * self.k:.1f} {960 - p[1] * self.k:.1f}"

    def _moveTo(self, p):
        self.out.append("M " + self.pt(p))

    def _lineTo(self, p):
        self.out.append("L " + self.pt(p))

    def _curveToOne(self, a, b, c):
        self.out.append("C " + " ".join(self.pt(p) for p in (a, b, c)))

    def _qCurveToOne(self, q, p):   # quadratic to cubic
        p0 = self._getCurrentPoint()
        a = (p0[0] + 2 / 3 * (q[0] - p0[0]), p0[1] + 2 / 3 * (q[1] - p0[1]))
        b = (p[0] + 2 / 3 * (q[0] - p[0]), p[1] + 2 / 3 * (q[1] - p[1]))
        self._curveToOne(a, b, p)

    def _closePath(self):
        self.out.append("Z")


def main() -> None:
    src = sys.argv[1] if len(sys.argv) > 1 else FONT
    font = TTFont(src)
    rev: dict[str, str] = {}
    for code, glyph in sorted(font.getBestCmap().items()):
        rev.setdefault(glyph, chr(code).lower())
    found: dict[str, str] = {}
    for lookup in font["GSUB"].table.LookupList.Lookup:
        for st in lookup.SubTable:
            st = getattr(st, "ExtSubTable", st)
            for first, ligs in getattr(st, "ligatures", {}).items():
                for lig in ligs:
                    name = "".join(rev.get(g, "?") for g in [first, *lig.Component])
                    if name in NAMES:
                        found[name] = lig.LigGlyph
    assert set(found) == set(NAMES), f"not in the font: {set(NAMES) - set(found)}"
    lines = []
    for wght, suffix in ((400, ""), (500, "-w500")):   # both weights: 400 is the default, 500 reads firmer at 16 px
        inst = instancer.instantiateVariableFont(TTFont(src), {"wght": wght, "GRAD": 0, "opsz": 24, "FILL": 0})
        gs = inst.getGlyphSet()
        k = 960 / inst["head"].unitsPerEm
        for name in NAMES:
            pen = PathPen(gs, k)
            gs[found[name]].draw(pen)
            lines.append(f"{name}{suffix}|" + " ".join(pen.out))
    (HERE / "glyphs.txt").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print("wrote glyphs.txt:", ", ".join(NAMES))


if __name__ == "__main__":
    main()
