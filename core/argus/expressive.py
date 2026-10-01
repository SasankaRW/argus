"""Ari's spoken style: a mood for each reply and a few sounds people make ([laugh], [sigh], ...).

The model writes a reply like "[cheerful] Oh nice, you finally fixed it! [laugh] What was it?". The expressive
voice (voice_server.py, Chatterbox) hears the mood as how lively to sound and the sounds as real laughs and sighs;
Piper and the screen get the words alone. Only plain standard-library code here: the voice server imports it from
its own environment.
"""

from __future__ import annotations

import re

MOODS = ("neutral", "cheerful", "excited", "playful", "calm", "sympathetic", "serious")
# The sounds Chatterbox-Turbo really makes (others are tokenised but silent, resemble-ai/chatterbox#557)
SOUNDS = ("laugh", "chuckle", "sigh", "gasp", "groan", "clear throat")
# mood -> how lively (Chatterbox exaggeration), pacing (cfg_weight: lower is slower), sampling temperature
STYLE = {
    "neutral": (0.5, 0.5, 0.8),
    "cheerful": (0.65, 0.45, 0.85),
    "excited": (0.8, 0.35, 0.9),
    "playful": (0.7, 0.4, 0.9),
    "calm": (0.35, 0.5, 0.7),
    "sympathetic": (0.4, 0.3, 0.7),
    "serious": (0.35, 0.55, 0.7),
}
# Piper can't do moods; a touch of speed is all it gets
PIPER_SPEED = {"excited": 1.08, "cheerful": 1.04, "playful": 1.04, "calm": 0.95, "sympathetic": 0.93,
               "serious": 0.97}

_MOOD = re.compile(r"^\s*\[(" + "|".join(MOODS) + r")\]\s*", re.I)
_TAG = re.compile(r"\s*\[(?:" + "|".join(MOODS + SOUNDS) + r"|laughs|sighs|chuckles)\]\s*", re.I)
_BAD = re.compile(r"\s*\[(?:whisper(?:ing)?|angry|fear|surprised|crying|happy|sad|sarcastic|dramatic|"
                  r"narration|advertisement|pause|breath|cough|sniff|shush)\]\s*", re.I)


def mood_of(text: str) -> tuple[str, str]:
    """("cheerful", the rest) for a reply starting with a mood tag; ("neutral", text) otherwise."""
    m = _MOOD.match(text or "")
    return (m.group(1).lower(), text[m.end():]) if m else ("neutral", text or "")


def plain(text: str) -> str:
    """The words alone (for the screen, Piper, the popup): no mood, no [laugh]."""
    t = _TAG.sub(" ", _BAD.sub(" ", text or ""))
    t = re.sub(r"\s+([.,!?])", r"\1", re.sub(r"\s{2,}", " ", t)).strip()
    return t


def for_voice(text: str) -> tuple[str, str]:
    """(mood, text with only the sounds Chatterbox makes, written its way: "[laugh]")."""
    mood, rest = mood_of(text)
    rest = _BAD.sub(" ", rest)
    rest = re.sub(r"\[(laughs)\]", "[laugh]", rest, flags=re.I)
    rest = re.sub(r"\[(sighs)\]", "[sigh]", rest, flags=re.I)
    rest = re.sub(r"\[(chuckles)\]", "[chuckle]", rest, flags=re.I)
    rest = re.sub(r"\[(" + "|".join(MOODS) + r")\]\s*", "", rest, flags=re.I)  # a mood mid-reply: dropped
    return mood, re.sub(r"\s{2,}", " ", rest).strip()


def tidy(reply: str, mood: str) -> str:
    """A model's reply and mood as Ari stores it: "[mood] words" (no tag for neutral), at most two sounds."""
    mood = (mood or "").strip().lower()
    _, words = mood_of(reply)
    n = 0

    def keep(m: re.Match) -> str:
        nonlocal n
        n += 1
        return m.group(0) if n <= 2 else " "

    words = re.sub(r"\[(?:" + "|".join(SOUNDS) + r")\]", keep, words, flags=re.I)
    words = re.sub(r"\s{2,}", " ", _BAD.sub(" ", words)).strip()
    return f"[{mood}] {words}" if mood in MOODS and mood != "neutral" and words else words
