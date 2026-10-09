"""Helping Whisper with your words: names, apps and how it tends to mishear you.

Whisper hears "Kaancha" as "Kancha" and "WhatsApp" as "what's up" because it has never heard you say them. Two
cheap fixes that need no training:

1. Vocabulary: the names Ari should expect (Argus's own, `ari.vocabulary`, and the names in what Ari remembers) go to
   Whisper as hotwords when it writes down a request, which biases it towards those spellings. Not as its prompt:
   on noise Whisper reads a prompt back ("about Do Not, Disturb, PC, Bluetooth") and Ari answered it. What comes
   back as just that list is thrown away (`echo`). The wake check gets no names at all, so it isn't nudged
   towards hearing "Ari".
2. Corrections: `ari.heard_as` maps what Whisper writes to what you meant ("kancha": "Kaancha"), plus a few
   built-in ones that only apply where they make sense ("open what's up" -> "open WhatsApp", but "what's up with
   the server?" stays).

Applied to everything Ari hears (the PC's microphone, Whisper in Helios) and to what Argus receives.
"""

from __future__ import annotations

import re

BUILTIN = ["Ari", "Argus", "Helios", "WhatsApp", "YouTube", "Spotify", "Brave", "Chrome", "VS Code", "Discord",
           "Telegram", "Gmail", "Tailscale", "SearXNG", "Ollama"]
COMMON = {"I", "I'm", "I've", "The", "A", "An", "My", "Mr", "Mrs", "Ms", "Dr", "On", "In", "At", "It", "We", "He",
          "She", "They", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday", "January",
          "February", "March", "April", "May", "June", "July", "August", "September", "October", "November",
          "December", "Sri", "Lanka"}
# misheard -> meant, only in the context of the pattern (group "x" is replaced)
CONTEXT = [
    (re.compile(r"\b(?:open|on|in|via|message|text|whatsapp)\s+(?P<x>what'?s\s?up|whats\s?app|what\s?sapp)\b", re.I),
     "WhatsApp"),
    (re.compile(r"(?P<x>\bwhats\s?app\b|\bwhat\s?sapp\b)", re.I), "WhatsApp"),
    (re.compile(r"^\W*(?:hey|hi|ok|okay)[\s,]+(?P<x>harry|hari|arie|are you|r[iy]|aria)\b", re.I), "Ari"),
]


def words(extra: list[str], facts: list[str], limit: int = 40) -> list[str]:
    """The names to expect: your `ari.vocabulary` first, then names in what Ari remembers, then Argus's own."""
    seen: dict[str, None] = {}
    for w in extra:
        if w.strip():
            seen.setdefault(w.strip(), None)
    for f in facts:
        for i, tok in enumerate(re.findall(r"[A-Z][\w'-]+(?:\s+[A-Z][\w'-]+)?", f)):
            if tok.split()[0] not in COMMON and not (i == 0 and f.startswith(tok) and tok.lower() in {"user"}):
                seen.setdefault(tok, None)
    for w in BUILTIN:
        seen.setdefault(w, None)
    return list(seen)[:limit]


def prompt(names: list[str]) -> str:
    """Whisper's initial prompt: a short line naming what it will hear."""
    return ("Talking to Ari about " + ", ".join(names) + ".")[:400] if names else ""


def echo(text: str, names: list[str]) -> bool:
    """True when Whisper only read the names back instead of hearing speech: "about Do Not, Disturb, PC, Zoom",
    "Argus, Helios, WhatsApp, Snapchat, Snapchat", "Talking to Ari about ...". Real requests use few of them."""
    t = text.strip().strip(".!?").strip()
    if not t:
        return False
    if re.match(r"^(?:talking to ari\b|about\b)", t, re.I) and "," in t:
        return True
    known = {n.lower() for n in names} | {w.lower() for n in names for w in n.split()}
    parts = [p.strip().lower() for p in re.split(r",", re.sub(r"^(?:talking to ari )?about ", "", t, flags=re.I))]
    parts = [p for p in parts if p]
    if len(parts) >= 3 and sum(p in known for p in parts) >= max(3, int(0.6 * len(parts))):
        return True
    words = re.findall(r"[\w']+", t.lower())
    return len(words) >= 4 and sum(w in known for w in words) / len(words) >= 0.75


def hotwords(names: list[str]) -> str:
    """The names as Whisper's hotwords (only for writing down a request)."""
    return ", ".join(names)[:300]


def fix(text: str, heard_as: dict[str, str] | None = None) -> str:
    """What you said, with Whisper's usual mishearings put right."""
    t = text
    for wrong, right in (heard_as or {}).items():
        if wrong.strip():
            t = re.sub(rf"(?<!\w){re.escape(wrong.strip())}(?!\w)", right, t, flags=re.I)
    for pat, right in CONTEXT:
        t = pat.sub(lambda m, right=right: m.group(0)[:m.start("x") - m.start()] + right
                    + m.group(0)[m.end("x") - m.start():], t)
    return t
