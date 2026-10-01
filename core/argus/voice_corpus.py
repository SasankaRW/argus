"""What to read aloud when you train Ari on your voice (Helios > Ari > Train on my voice).

About 300 short sentences, so 20-30 minutes of reading: the things you say to Ari (commands, questions, follow-ups),
casual talk, numbers and times, and the names Ari should know (your `ari.vocabulary` and the names it remembers)
in many sentences each, since names are what Whisper gets wrong most. The order is fixed (so a sentence keeps its
id), mixed so that any first 10 minutes already cover a bit of everything.
"""

from __future__ import annotations

import hashlib
import itertools

COMMANDS = [
    "Hey Ari, what's running right now?",
    "Hey Ari, open Spotify and turn the volume down.",
    "Open Brave.",
    "Open WhatsApp and send a message to {name} saying I'm on my way.",
    "Text {name} on WhatsApp that I'll call later.",
    "Remind me to call {name} tomorrow at five pm.",
    "Sort my downloads every morning at seven.",
    "Shut down the PC at eleven tonight.",
    "What's on my screen?",
    "What does this error say?",
    "Sum up what I copied.",
    "Find my CV.",
    "Find the PDFs I changed today.",
    "Search my files for the laptop server notes.",
    "How's the weather today?",
    "Will it rain tomorrow in {place}?",
    "What's the weather like in {place} this evening?",
    "Add milk, eggs and bread to the shopping list.",
    "What's on my shopping list?",
    "How much did I spend this month?",
    "What's urgent at work?",
    "Add an issue: the login page is slow, priority one.",
    "Move that issue to done.",
    "Start a timer on the payments issue.",
    "Is the home lab up?",
    "Did last night's backup finish?",
    "What changed in my repos today?",
    "Run the morning routine.",
    "Take a note: buy a new charger for the laptop.",
    "What did I note about the server?",
    "Watch this page and tell me when the price drops.",
    "Save this for later.",
    "What's on my reading list?",
    "Merge these PDFs into one.",
    "Shrink these pictures so I can email them.",
    "Pause the music.",
    "Skip this song.",
    "Turn the volume up a bit.",
    "Mute the PC.",
    "Close Chrome.",
    "Switch to Visual Studio Code.",
    "Where's my phone?",
    "Ring my phone.",
    "Turn on the torch on my phone.",
    "Set a timer for ten minutes.",
    "How did my week go?",
    "Good morning, Ari.",
    "Brief me.",
    "Remember that {name} lives in {place}.",
    "Forget what I said about the dentist.",
    "What do you remember about me?",
    "Search the web for the best budget headphones.",
    "Read that page and tell me the gist.",
    "Who won the cricket match yesterday?",
    "Wake up the PC.",
    "Put the PC to sleep.",
    "Restart the worker.",
    "Show me the queue.",
    "What failed today?",
    "Approve it.",
    "Yes, do it.",
    "No, leave it.",
    "Stop.",
    "Wait, never mind.",
    "Thanks Ari, that's all.",
    "And tomorrow?",
    "And what about the other one?",
    "Do that again.",
    "Open the folder it's in.",
    "Send that to my phone.",
]

TALK = [
    "Hey, how are you doing today?",
    "I'm a bit tired, it was a long day at work.",
    "Tell me a joke.",
    "That's actually pretty funny.",
    "What do you think about pineapple on pizza?",
    "I'm bored, say something interesting.",
    "Guess what happened at the office today.",
    "Not much, just chilling at home.",
    "I finally fixed that bug I was stuck on all week.",
    "Do you ever get tired of answering questions?",
    "Let's just talk for a bit.",
    "I'm going to make some tea, want to hear about my day after?",
    "The traffic in Colombo was terrible this morning.",
    "It's been raining nonstop since the afternoon.",
    "I watched a really good movie last night.",
    "My exam results come out next week.",
    "We had rice and curry for lunch again.",
    "I need a holiday, maybe somewhere near the beach.",
    "Honestly, I could sleep for twelve hours.",
    "That's cool, tell me more.",
    "No way, are you serious?",
    "Okay, good night Ari.",
    "Bro, you won't believe this.",
    "Machan, what's the plan for the weekend?",
    "Let's go for a ride to {place} on Sunday.",
    "I was talking to {name} about the new project.",
    "{name} said the meeting moved to Thursday.",
    "I'm meeting {name} in {place} after work.",
]

NUMBERS = [
    "The meeting is at nine thirty on Wednesday the fourteenth.",
    "Set an alarm for six forty five.",
    "It costs two thousand four hundred and fifty rupees.",
    "My number ends in seven seven three one.",
    "The flight leaves at 11:20 pm on the 3rd of March.",
    "Download speed is about eighty megabits per second.",
    "Twelve, thirteen, fourteen, fifteen, sixteen.",
    "The file is three point two gigabytes.",
    "Version zero point seven point one is out.",
    "Room four oh six, third floor.",
    "Remind me in twenty minutes.",
    "Every Monday and Friday at eight in the morning.",
    "The total came to forty two dollars and ninety cents.",
    "Port eight six hundred on the laptop.",
    "It's thirty one degrees outside.",
]

# Varied everyday sentences that cover the sounds of English (written for Argus).
PLAIN = [
    "The quick brown dog jumped over a lazy fox by the river.",
    "She sells fresh fish at the market every Saturday morning.",
    "A cold breeze came through the open window last night.",
    "Please check the oven before you leave the house.",
    "The children played cricket until the sun went down.",
    "He thought the theory was thorough but rather thin.",
    "We usually visit our grandparents in the village in April.",
    "The red car parked outside has a flat tyre.",
    "Could you bring two cups of strong coffee, please?",
    "The judge measured the edge of the bridge with a ruler.",
    "Very few people ever visit that valley in winter.",
    "Which of these watches would you wear to a wedding?",
    "Our new neighbour plays the violin every evening.",
    "The shop on the corner sells sweets and newspapers.",
    "Thirty three thousand birds flew south this year.",
    "I usually take the bus, but today I walked.",
    "The pleasure of reading is hard to measure.",
    "Change the oil and check the engine before the trip.",
    "The garage door squeaks whenever it rains.",
    "Jumbo jets and gentle giraffes are both very large.",
    "Yellow lights glowed along the quiet lagoon.",
    "Zebras zigzag across the dusty plain.",
    "The thick fog hid the hills behind the town.",
    "Fresh mangoes are sweeter in the dry season.",
    "Please don't forget to lock the back gate.",
    "The engineer explained the problem in simple words.",
    "A shiny new bicycle stood against the wall.",
    "My brother broke his phone screen again.",
    "We'll need batteries, tape and a small screwdriver.",
    "The library closes early on public holidays.",
    "Their house is near the temple, past the school.",
    "Whether or not it rains, the match will go ahead.",
    "The kettle whistled while the toast was burning.",
    "Turn left at the junction and go straight for a kilometre.",
    "The elephants walked slowly to the water.",
    "An orange cat was sleeping on the warm roof.",
    "Ask the waiter for the bill when you're ready.",
    "The program crashed because the disk was full.",
    "Hit save before you close the editor.",
    "The update installs overnight and restarts the server.",
]

PLACES = ["Colombo", "Kandy", "Galle", "Moratuwa", "Negombo", "Jaffna", "Nuwara Eliya", "Ella", "Kurunegala",
          "Matara", "Anuradhapura", "Trincomalee"]
NAMES = ["Kaancha", "Nimali", "Kasun", "Dilini", "Tharindu", "Sachini", "Nuwan", "Ishara"]
APPS = ["WhatsApp", "Spotify", "Brave", "Chrome", "VS Code", "Discord", "Telegram", "Gmail", "YouTube",
        "File Explorer", "Task Manager", "Docker Desktop", "Steam", "Notion", "Obsidian"]
PERSON_LINES = [
    "Call {w} when you get a chance.",
    "Send {w} a message on WhatsApp.",
    "What did {w} say yesterday?",
    "Remind me to text {w} after lunch.",
    "Tell {w} I'll be late.",
    "Is it {w}'s birthday this week?",
    "I'm having dinner with {w} on Friday.",
    "Search my notes for anything about {w}.",
]
APP_LINES = [
    "Open {w}.",
    "Is {w} running?",
    "Close {w} please.",
    "Switch to {w}.",
]
PLACE_LINES = [
    "What's the weather in {w} tomorrow?",
    "How long does it take to get to {w}?",
    "I'm going to {w} this weekend.",
    "Find a good place to eat in {w}.",
]
MORE_PLAIN = [
    "Small boats bobbed gently in the harbour.",
    "The printer jammed halfway through the report.",
    "Keep the receipt in case you need to return it.",
    "Grandmother's recipe uses coconut milk and curry leaves.",
    "The train to Kandy was packed this morning.",
    "Lightning struck the old tree behind the school.",
    "Push the button twice to reset the router.",
    "Nobody noticed the window was still open.",
    "The puppy chewed through my charging cable.",
    "Wash the vegetables before you cut them.",
    "The cinema was half empty on Tuesday night.",
    "The heavy suitcase barely fit in the boot.",
    "Clouds gathered quickly over the mountains.",
    "My laptop fan gets loud when I compile the project.",
    "We waited forty minutes for the bus to come.",
    "The beach was crowded during the long weekend.",
    "Bring a jacket, it gets chilly up in the hills.",
    "The new café serves really good hoppers.",
    "Plug the monitor into the second port.",
    "Our team shipped the release a day early.",
    "Somebody left a voice message on the office phone.",
    "The milk went sour because the fridge was off.",
    "Each answer should be short and clear.",
    "Turn the page and read the next paragraph aloud.",
    "The power cut lasted almost three hours.",
    "He jogged around the lake before breakfast.",
    "Write the password on paper and keep it safe.",
    "The bakery sells out of bread by noon.",
    "Thunder rumbled in the distance all evening.",
    "The market is busiest on Sunday mornings.",
    "The kids built a sandcastle near the water.",
    "Measure twice and cut once.",
    "The station clock was five minutes slow.",
    "I misplaced my keys somewhere in the living room.",
    "The tea plantation stretched across the valley.",
    "Please speak a little slower on the call.",
    "The warranty covers the battery for two years.",
    "That old radio still works perfectly.",
    "The garden needs watering every other day.",
    "Their flight was delayed because of the storm.",
]


def sid(sentence: str) -> str:
    return hashlib.sha1(sentence.encode()).hexdigest()[:12]


def sentences(vocabulary: list[str] | None = None) -> list[dict]:
    """[{id, text, kind}], about 300, in recording order. Your vocabulary goes first among the names."""
    words = [w for w in (vocabulary or []) if w.strip()]
    names = list(dict.fromkeys([w for w in words if w not in APPS and w not in PLACES] + NAMES))
    places = PLACES
    out: list[dict] = []
    seen: set[str] = set()

    def add(text: str, kind: str) -> None:
        if text not in seen:
            seen.add(text)
            out.append({"id": sid(text), "text": text, "kind": kind})

    n = itertools.cycle(names)
    p = itertools.cycle(places)
    fill = lambda s: s.format(name=next(n), place=next(p))  # noqa: E731
    people = [(t.format(w=w), "names") for w in names for t in PERSON_LINES[hash_i(w) % 2::2]]
    things = [(t.format(w=w), "names") for w in APPS for t in APP_LINES[hash_i(w) % 2::2]]
    towns = [(t.format(w=w), "names") for w in places for t in PLACE_LINES[hash_i(w) % 2::2]]
    groups = [
        [(fill(s), "command") for s in COMMANDS],
        [(fill(s), "talk") for s in TALK],
        [(s, "numbers") for s in NUMBERS],
        [(s, "plain") for s in PLAIN + MORE_PLAIN],
        people, things, towns,
        [(fill(s), "command") for s in COMMANDS if "{" in s] + [(fill(s), "talk") for s in TALK if "{" in s],
    ]
    for row in itertools.zip_longest(*groups):  # mixed: a command, a chat line, a number, ...
        for item in row:
            if item:
                add(*item)
    return out


def hash_i(s: str) -> int:
    return int(hashlib.sha1(s.encode()).hexdigest()[:6], 16)
