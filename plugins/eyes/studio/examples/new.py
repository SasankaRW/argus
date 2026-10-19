"""The 'something new' clip: an app the eyes haven't seen before."""
from clipkit import make

ABOUT = "something new: notices, peeks around, studies it, tilts, wow, thinks, likes it"
POINTS = {}
BEATS = [
    ("home", "neutral", 1.8, {}),
    ("oh?", "notice", 2.4, {}),
    ("peek up-left", "notice", 2.8, {"track": "upL"}),
    ("peek right", "curious", 3.0, {"track": "farR"}),
    ("home", "neutral", 1.8, {}),
    ("study it", "suspicious", 3.6, {"track": "nearR"}),
    ("tilt", "curious", 3.2, {"over": "tilt", "track": "down"}),
    ("home", "neutral", 1.8, {}),
    ("wow", "wow", 2.6, {"track": "up"}),
    ("think", "neutral", 3.2, {"over": "think", "track": "upL"}),
    ("likes it", "happy", 3.2, {}),
    ("home", "neutral", 1.8, {}),
    ("content", "content", 2.8, {"blink": [1.2]}),
    ("home", "neutral", 1.8, {}),
]
STORY = make(BEATS, POINTS)
