"""Helping Whisper with your words: names, apps and how it tends to mishear you.

Whisper hears "Kaancha" as "Kancha" and "WhatsApp" as "what's up" because it has never heard you say them. Two
cheap fixes that need no training:

1. Vocabulary: the names Ari should expect (Argus's own, `ari.vocabulary`, and the names in what Ari remembers) go to
   Whisper as its prompt and hotwords, which biases it towards those spellings.
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
