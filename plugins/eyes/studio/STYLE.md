# Making an eyes clip

The clip plays on a small screen (320 x 240) on the PC's water cooler while one app is in front, over and over,
for as long as you use that app. It is a pair of robot eyes, white on black, and nothing else.

## How a clip is made

A clip is a Python file with three things (copy `examples/default.py` and change them):

- `ABOUT`: one short line saying what the eyes do, like the examples ("coding: reads along, focuses, thinks,
  pleased when it works"). Other apps may reuse your clip because of this line.
- `POINTS`: extra places to look at, if the built-in ones aren't enough. The screen is 400 x 300 here; the eyes
  sit at (125, 150) and (275, 150). Built in: centre, farL, farR, nearL, nearR, up, upL, upR, down, downL, downR.
- `BEATS`: what happens, in order: `(tag, shape, seconds, options)`. The shapes and options are listed at the top of
  `clipkit.py`. The tag is a short label for you; tags starting with `home` are the shared resting pose.

`STORY = make(BEATS, POINTS)` at the end, as in the examples.

## The style (this matters most)

- **Slow and calm.** It plays for hours: nothing hurried, nothing that flickers. A beat is usually 2.5 to 4 s.
  Never more than two emotions in a row without settling (a calm beat or home in between).
- **Come home often.** A `home` beat (1.6 to 2 s, `neutral`, no options) at the start, at the end, and at least every
  12 s. The player only switches clips at home, so long stretches away make switching slow.
- **30 to 60 s** in total.
- **About the app.** Make it feel like the eyes are doing what you do there: watching a video (looking at the middle,
  amused, wow, content), reading mail (reading left to right, a little notice, a nod of content), coding (reading,
  focus, thinking upward, pleased when it works), music (bliss, content, gentle looking around)... Mostly gentle,
  happy or curious feelings. Avoid angry, sad and surprised unless they really fit; use love and squash rarely.
- **Eyes only.** No props, no colour, no text: `clipkit.make` draws only the eyes.
- Different from the examples: same calm style, but this app's own little story.

## Working

1. `python sheet.py <name>.py` renders a picture every 1.5 s into `sheet.png` (and lists the beats). Read
   `sheet.png` and look at it: does each moment read as you meant? Is anything rushed or odd?
   `python sheet.py <name>.py --at 12.4 12.6 12.8` shows chosen moments larger (`zoom.png`).
2. Change the beats and look again until it is good (usually two or three rounds).
3. `python render.py <name>.py` renders the whole clip and runs the checks Argus runs before saving it. Fix anything
   it lists.

Don't change `eyecore.py`, `clipkit.py`, `sheet.py` or `render.py` (Argus puts fresh copies back before it renders).
