"""Ari's spoken style: a mood for each reply and a few sounds people make ([laugh], [sigh], ...).

The model writes a reply like "[cheerful] Oh nice, you finally fixed it! [laugh] What was it?". Ari's voice
(voice_server.py, Chatterbox) hears the mood as how lively to sound and the sounds as real laughs and sighs; the
screen gets the words alone. Only plain standard-library code here: the voice server imports it from
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
# Moods a model makes up ("[curious]", "[warm]") are not in the list: each maps to the nearest one, and any other
# tag is dropped. Left alone it was read out loud ("curious ...") and shown on the island.
ALIASES = {
    "curious": "playful", "amused": "playful", "teasing": "playful", "mischievous": "playful", "witty": "playful",
    "joking": "playful", "thoughtful": "calm", "gentle": "calm", "reflective": "calm", "thinking": "calm",
    "relaxed": "calm", "soothing": "calm", "warm": "cheerful", "friendly": "cheerful", "upbeat": "cheerful",
    "pleased": "cheerful", "enthusiastic": "excited", "thrilled": "excited", "delighted": "excited",
    "concerned": "sympathetic", "empathetic": "sympathetic", "worried": "sympathetic", "caring": "sympathetic",
    "apologetic": "sympathetic", "firm": "serious", "stern": "serious", "confident": "neutral",
    "casual": "neutral", "matter-of-fact": "neutral",
}
_BRACKET = re.compile(r"\[\s*([A-Za-z][A-Za-z -]{0,19}?)\s*\]")
_SOUND_WORDS = {*SOUNDS, "laughs", "sighs", "chuckles"}


def _fix_tag(m: re.Match) -> str:
    w = m.group(1).strip().lower()
    if w in MOODS or w in _SOUND_WORDS:
        return m.group(0)
    return f"[{ALIASES[w]}]" if w in ALIASES else " "


def normalise(text: str) -> str:
    """The text with made-up mood tags mapped to a real mood and unknown tags dropped."""
    return re.sub(r" {2,}", " ", _BRACKET.sub(_fix_tag, text or ""))


def mood_name(mood: str) -> str:
    """A mood word from a model as one of MOODS ("" when it isn't one and has no near match)."""
    w = (mood or "").strip().lower()
    return w if w in MOODS else ALIASES.get(w, "")


_MOOD = re.compile(r"^\s*\[(" + "|".join(MOODS) + r")\]\s*", re.I)
_TAG = re.compile(r"\s*\[(?:" + "|".join(MOODS + SOUNDS) + r"|laughs|sighs|chuckles)\]\s*", re.I)
_BAD = re.compile(r"\s*\[(?:whisper(?:ing)?|angry|fear|surprised|crying|happy|sad|sarcastic|dramatic|"
                  r"narration|advertisement|pause|breath|cough|sniff|shush)\]\s*", re.I)


def mood_of(text: str) -> tuple[str, str]:
    """("cheerful", the rest) for a reply starting with a mood tag; ("neutral", text) otherwise."""
    text = normalise(text)
    m = _MOOD.match(text)
    return (m.group(1).lower(), text[m.end():]) if m else ("neutral", text.strip())


def plain(text: str) -> str:
    """The words alone (for the screen, the popup): no mood, no [laugh]."""
    t = _TAG.sub(" ", _BAD.sub(" ", normalise(text)))
    t = re.sub(r"\s+([.,!?])", r"\1", re.sub(r"\s{2,}", " ", t)).strip()
    return t


_ANY_MOOD = re.compile(r"\[(" + "|".join(MOODS) + r")\]", re.I)


def phrases(text: str, mood: str = "neutral") -> list[tuple[str, str]]:
    """A reply cut where its mood changes: "[excited] We won! [sympathetic] Shame about the rain." ->
    [("excited", "We won!"), ("sympathetic", "Shame about the rain.")]. A mood holds until the next tag; text before
    the first tag gets `mood`. Sounds ([laugh]) stay with their words."""
    text = normalise(text)
    out: list[tuple[str, str]] = []
    pos = 0
    for m in _ANY_MOOD.finditer(text):
        chunk = text[pos:m.start()].strip()
        if chunk:
            out.append((mood, chunk))
        mood, pos = m.group(1).lower(), m.end()
    chunk = text[pos:].strip()
    if chunk:
        out.append((mood, chunk))
    merged: list[tuple[str, str]] = []
    for md, t in out:  # the same mood twice in a row: one phrase
        if merged and merged[-1][0] == md:
            merged[-1] = (md, f"{merged[-1][1]} {t}")
        else:
            merged.append((md, t))
    return merged


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
    mood = mood_name(mood)
    _, words = mood_of(reply)
    n = 0

    def keep(m: re.Match) -> str:
        nonlocal n
        n += 1
        return m.group(0) if n <= 2 else " "

    words = re.sub(r"\[(?:" + "|".join(SOUNDS) + r")\]", keep, words, flags=re.I)
    words = re.sub(r"\s{2,}", " ", _BAD.sub(" ", words)).strip()
    trailing = r"(?:\s*\[(?:" + "|".join(MOODS) + r")\])+$"  # a mood with nothing after it
    words = re.sub(trailing, "", words, flags=re.I).strip()
    return f"[{mood}] {words}" if mood in MOODS and mood != "neutral" and words else words
