"""Cut Material Symbols Rounded down to the icons the phone view uses (src/icons.ts), so Helios ships ~20 KB, not 5 MB.

    pip install fonttools brotli
    python scripts/icons.py        (from helios/, after npm ci; writes src/fonts/symbols-rounded.woff2)

Run it again after adding a name to ICONS in src/icons.ts. The font is pinned to weight 500 and grade 0 at 24 px
optical size, and keeps the FILL axis (0 = outlined, 1 = filled), as the Sonoma spec asks.
"""

import re
from pathlib import Path

from fontTools import subset
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

HERE = Path(__file__).resolve().parents[1]
SRC = HERE / "node_modules" / "material-symbols" / "material-symbols-rounded.woff2"
OUT = HERE / "src" / "fonts" / "symbols-rounded.woff2"

listing = (HERE / "src" / "icons.ts").read_text(encoding="utf-8").split("ICONS = [", 1)[1].split("]", 1)[0]
names = re.findall(r'"([a-z0-9_]+)"', listing)
font = TTFont(SRC)
rev: dict[str, str] = {}
for code, glyph in sorted(font.getBestCmap().items()):
    rev.setdefault(glyph, chr(code).lower())  # 'A' and 'a' share a glyph
# Every icon is a ligature of its name's letters (rlig); find the glyph each wanted name turns into.
want: set[str] = set()
for lookup in font["GSUB"].table.LookupList.Lookup:
    for st in lookup.SubTable:
        st = getattr(st, "ExtSubTable", st)
        for first, ligs in getattr(st, "ligatures", {}).items():
            for lig in ligs:
                text = "".join(rev.get(g, "?") for g in [first, *lig.Component])
                if text in names:
                    want.add(lig.LigGlyph)
missing = len(names) - len(want)
assert missing == 0, f"{missing} icon name(s) in src/icons.ts are not in Material Symbols"
chars = sorted({c for n in names for c in n})
opts = subset.Options()
opts.layout_features = ["rlig", "rclt", "liga"]
opts.layout_closure = False  # only the ligatures asked for, not every icon spelled with the same letters
opts.flavor = None
opts.notdef_outline = False
sub = subset.Subsetter(opts)
sub.populate(glyphs=sorted(want), unicodes=[ord(c) for c in chars])
sub.subset(font)
font = instancer.instantiateVariableFont(font, {"wght": 500, "GRAD": 0, "opsz": 24})
font.flavor = "woff2"
font.recalcTimestamp = False  # the same names give the same file (the built Helios stays reproducible)
OUT.parent.mkdir(parents=True, exist_ok=True)
font.save(OUT)
print(f"{len(names)} icons -> {OUT.relative_to(HERE)} ({OUT.stat().st_size // 1024} KB)")
