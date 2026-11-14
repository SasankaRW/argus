"""Make the fonts Ari's island draws with (core/argus/island_fonts/*.ttf), small enough to ship in the package.

    pip install fonttools brotli
    python scripts/island_fonts.py       (after `npm ci` in helios/, for the material-symbols package)

Qt draws static TTFs most reliably, so each weight is its own file, in its own family name:
  Inter, Inter Medium, Inter SemiBold  Inter 4.1 (helios/src/fonts/InterVariable.woff2) at text size, with the
                                       Sonoma alternates ss01 + cv11 (open digits, single-storey a) baked in
  Inter Display                        weight 640 at display size, for the clock
  Symbols, Symbols Filled              Material Symbols Rounded cut to the names in ICONS, weight 500, outlined
                                       and filled (Qt turns a name into its icon through the font's ligatures)
Add a name to ICONS, run this again, and commit the fonts.
"""

from __future__ import annotations

from pathlib import Path

from fontTools import subset
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "core" / "argus" / "island_fonts"
INTER = ROOT / "helios" / "src" / "fonts" / "InterVariable.woff2"
SYMBOLS = ROOT / "helios" / "node_modules" / "material-symbols" / "material-symbols-rounded.woff2"

# Every icon the island draws (core/argus/ari_popup.py, ICON_* and icon_for).
ICONS = ["mic", "hub", "sports_esports", "power_settings_new", "play_arrow", "pause", "skip_previous", "skip_next",
         "chevron_right", "memory", "music_note", "inbox", "grid_view", "chat_bubble", "description",
         "bedtime", "auto_awesome", "bolt", "language", "notifications", "circle"]
# Latin, Latin-1, general punctuation and the few symbols the island prints.
TEXT = ([*range(0x20, 0x7F), *range(0xA0, 0x100), 0x2013, 0x2014, 0x2018, 0x2019, 0x201C, 0x201D, 0x2026, 0x2022,
         0x2190, 0x2191, 0x2193, 0x2192, 0x203A, 0x2713, 0x266A, 0xB7, 0xB0])
FEATURES = ("ss01", "cv11")


def set_names(font: TTFont, family: str) -> None:
    """One family per file, always 'Regular' weight 400: no style matching to go wrong."""
    name = font["name"]
    for rec in list(name.names):
        if rec.nameID in (16, 17, 21, 22, 25) or rec.nameID in (1, 2, 3, 4, 6):
            name.removeNames(nameID=rec.nameID)
    ps = family.replace(" ", "")
    for nid, text in ((1, family), (2, "Regular"), (3, f"{ps};island"), (4, family), (6, f"{ps}-Regular")):
        name.setName(text, nid, 3, 1, 0x409)
        name.setName(text, nid, 1, 0, 0)
    font["OS/2"].usWeightClass = 400
    font["OS/2"].fsSelection = (font["OS/2"].fsSelection & ~0b1100001) | 0b1000000
    font["head"].macStyle = 0


def bake(font: TTFont, tags: tuple[str, ...]) -> None:
    """Make a feature's single substitutions the default (cmap), so no shaper has to switch them on."""
    gsub = font["GSUB"].table
    lookups = {i for f in gsub.FeatureList.FeatureRecord if f.FeatureTag in tags for i in f.Feature.LookupListIndex}
    cmap = font.getBestCmap()
    for i in lookups:
        for st in gsub.LookupList.Lookup[i].SubTable:
            st = getattr(st, "ExtSubTable", st)
            mapping = getattr(st, "mapping", None)
            if mapping:
                for code, glyph in list(cmap.items()):
                    if glyph in mapping:
                        cmap[code] = mapping[glyph]
    for table in font["cmap"].tables:
        if table.isUnicode():
            table.cmap.update({c: g for c, g in cmap.items() if c in table.cmap})


def inter(wght: float, opsz: float, family: str, file: str) -> None:
    font = TTFont(INTER)
    font = instancer.instantiateVariableFont(font, {"wght": wght, "opsz": opsz})
    opts = subset.Options()
    opts.layout_features = ["kern", "calt", "liga", "tnum", "ss01", "cv11"]
    opts.notdef_outline = True
    sub = subset.Subsetter(opts)
    sub.populate(unicodes=TEXT)
    sub.subset(font)
    bake(font, FEATURES)
    set_names(font, family)
    font.flavor = None
    font.save(OUT / file)


def symbols(fill: int, family: str, file: str) -> None:
    font = TTFont(SYMBOLS)
    rev: dict[str, str] = {}
    for code, glyph in sorted(font.getBestCmap().items()):
        rev.setdefault(glyph, chr(code).lower())
    want: set[str] = set()
    for lookup in font["GSUB"].table.LookupList.Lookup:
        for st in lookup.SubTable:
            st = getattr(st, "ExtSubTable", st)
            for first, ligs in getattr(st, "ligatures", {}).items():
                for lig in ligs:
                    if "".join(rev.get(g, "?") for g in [first, *lig.Component]) in ICONS:
                        want.add(lig.LigGlyph)
    assert len(want) == len(ICONS), f"{len(ICONS) - len(want)} icon name(s) are not in Material Symbols"
    opts = subset.Options()
    opts.layout_features = ["rlig", "rclt", "liga"]
    opts.layout_closure = False
    opts.notdef_outline = False
    sub = subset.Subsetter(opts)
    sub.populate(glyphs=sorted(want), unicodes=[ord(c) for c in sorted({c for n in ICONS for c in n})])
    sub.subset(font)
    font = instancer.instantiateVariableFont(font, {"wght": 500, "GRAD": 0, "opsz": 24, "FILL": fill})
    set_names(font, family)
    font.flavor = None
    font.recalcTimestamp = False
    font.save(OUT / file)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    inter(400, 14, "Inter", "Inter-Regular.ttf")
    inter(500, 14, "Inter Medium", "Inter-Medium.ttf")
    inter(600, 14, "Inter SemiBold", "Inter-SemiBold.ttf")
    inter(640, 32, "Inter Display", "Inter-Display.ttf")
    symbols(0, "Island Symbols", "Symbols-Rounded.ttf")
    symbols(1, "Island Symbols Filled", "Symbols-Rounded-Filled.ttf")
    for f in sorted(OUT.glob("*.ttf")):
        print(f"{f.name}: {f.stat().st_size // 1024} KB")
