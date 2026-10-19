"""The default clip: calm, for any app without a clip of its own."""
from clipkit import make

ABOUT = "calm default: looks around, blinks, tilts, smiles, thinks"
POINTS = {}
BEATS = [
    ("home", "neutral", 1.6, {}),
    ("look left", "curious", 3.0, {"track": "farL"}),
    ("look right", "curious", 3.0, {"track": "farR"}),
    ("home", "neutral", 1.8, {}),
    ("blinky", "content", 3.6, {"blink": [1.0, 2.6]}),
    ("home", "neutral", 1.8, {}),
    ("tilt", "curious", 3.4, {"over": "tilt", "track": "nearR"}),
    ("happy", "happy", 3.4, {}),
    ("home", "neutral", 1.8, {}),
    ("look up", "notice", 2.6, {"track": "up"}),
    ("think", "neutral", 3.0, {"over": "think", "track": "upL"}),
    ("home", "neutral", 1.8, {}),
    ("content", "content", 3.0, {}),
    ("home", "neutral", 1.6, {}),
]
STORY = make(BEATS, POINTS)
