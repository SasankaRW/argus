"""Scripted conversations for `python -m argus.ari_eval --conversations`: does Ari keep the thread?

Each conversation is a few things you'd say in a row. For every step: what Ari should do first (a tool, or "reply",
"chat", "web"; several allowed: "a|b"), the arguments that matter (each a word or two that must be in it, "a|b" for
either), and then what "happened" so the next step has its context: the tool's result (made up, nothing runs) and
what Ari said. The context is always the right one (the scripted turns, not what the model did), so each step is
tested on its own: a miss doesn't spoil the rest of the conversation.
"""

from __future__ import annotations

from typing import Any


def S(say: str, expect: str, args: dict[str, str] | None = None, said: str = "", result: Any = None,
      do: dict[str, Any] | None = None, asks: bool = False) -> dict[str, Any]:
    """One step. `do` is the arguments the tool really got (default: `args` as written); `asks` marks Ari's answer
    as a question waiting for a yes."""
    return {"say": say, "expect": expect, "args": args or {}, "said": said, "result": result,
            "do": do if do is not None else {k: v.split("|")[0] for k, v in (args or {}).items()}, "asks": asks}


MAILS = [{"n": 1, "from": "Kaancha", "subject": "Dinner on Saturday?"},
         {"n": 2, "from": "GitHub", "subject": "[argus] CI passed on main"},
         {"n": 3, "from": "Daraz", "subject": "Your order has shipped"}]
ISSUES = [{"key": "ACME-7", "title": "Login page times out", "status": "todo"},
          {"key": "ACME-9", "title": "Export to CSV", "status": "in_progress"}]

CONVERSATIONS: list[dict[str, Any]] = [
    {"name": "spotify: open, play, louder, pause", "steps": [
        S("open spotify", "open_app", {"name": "spotify"}, "Opening Spotify."),
        S("play Famous", "play_on_spotify", {"query": "famous"}, "Playing Famous."),
        S("louder", "set_volume", {}, "Turned it up.", do={"change": "up"}),
        S("pause it", "media_control", {"action": "play_pause"}, "Done.")]},
    {"name": "spotify: artist, next, turn it up", "steps": [
        S("open spotify", "open_app", {"name": "spotify"}, "Opening Spotify."),
        S("play some Coldplay", "play_on_spotify", {"query": "coldplay"}, "Here's Coldplay."),
        S("next", "media_control", {"action": "next"}, "Skipping."),
        S("turn it up a bit", "set_volume", {}, "Turned it up.", do={"change": "up"})]},
    {"name": "spotify, then something else", "steps": [
        S("open spotify", "open_app", {"name": "spotify"}, "Opening Spotify."),
        S("play Shape of You", "play_on_spotify", {"query": "shape of you"}, "Playing Shape of You."),
        S("what's the weather like", "weather", {}, "Sunny, 31 degrees.", result={"now": "sunny", "temp": 31})]},
    {"name": "mail: check, read the second", "steps": [
        S("check my emails", "check_email", {}, "Three new: Kaancha about dinner on Saturday, GitHub, and Daraz.",
          result=MAILS),
        S("read the second one", "read_email", {"which": "2|second"}, "GitHub says CI passed on main.")]},
    {"name": "mail: check, the one from Kaancha", "steps": [
        S("any new mail?", "check_email", {}, "Three new: Kaancha about dinner on Saturday, GitHub, and Daraz.",
          result=MAILS),
        S("what does Kaancha say", "read_email", {"which": "kaancha|1"},
          "She asks if you're free for dinner on Saturday.")]},
    {"name": "mail, music, back to mail", "steps": [
        S("check my emails", "check_email", {}, "Three new: Kaancha, GitHub and Daraz.", result=MAILS),
        S("open spotify", "open_app", {"name": "spotify"}, "Opening Spotify."),
        S("read the first email", "read_email", {"which": "1|first|kaancha"}, "Kaancha asks about dinner.")]},
    {"name": "file: find, open it", "steps": [
        S("find my CV", "search_everything|find_file|search_my_files", {"query": "cv"},
          "Found CV_2026.pdf in Documents.", result=[{"path": "C:/Users/Sas/Documents/CV_2026.pdf"}],
          do={"query": "cv"}),
        S("open it", "open_file", {"path": "cv_2026"}, "Opening it.")]},
    {"name": "file: find, open the second", "steps": [
        S("find the invoice pdfs", "search_everything|find_file|search_my_files", {"query": "invoice"},
          "Two: invoice-sept.pdf and invoice-oct.pdf in Downloads.",
          result=[{"path": "C:/Users/Sas/Downloads/invoice-sept.pdf"},
                  {"path": "C:/Users/Sas/Downloads/invoice-oct.pdf"}]),
        S("open the october one", "open_file", {"path": "invoice-oct"}, "Opening it.")]},
    {"name": "weather: today, tomorrow", "steps": [
        S("what's the weather", "weather", {}, "Sunny, 31 degrees.", result={"now": "sunny", "temp": 31}),
        S("and tomorrow?", "weather", {"tomorrow": "true"}, "Showers in the afternoon.")]},
    {"name": "weather: Kandy, then Colombo", "steps": [
        S("weather in Kandy", "weather", {"place": "kandy"}, "Kandy: cloudy, 24 degrees."),
        S("what about Colombo", "weather", {"place": "colombo"}, "Colombo: sunny, 30 degrees.")]},
    {"name": "whatsapp: the missing message", "steps": [
        S("message Kaancha on WhatsApp", "reply|whatsapp_message", {}, "What should I say to Kaancha?", do={}),
        S("tell her I'm running late", "whatsapp_message", {"to": "kaancha", "text": "late"},
          "Shall I message Kaancha \"I'm running late\" on WhatsApp?", asks=True)]},
    {"name": "whatsapp: text, then again to someone else", "steps": [
        S("text Kaancha I'm on my way on WhatsApp", "whatsapp_message", {"to": "kaancha", "text": "on my way"},
          "Shall I message Kaancha \"I'm on my way\" on WhatsApp?", asks=True),
        S("send the same to Nimali", "whatsapp_message", {"to": "nimali", "text": "on my way"},
          "Shall I message Nimali \"I'm on my way\" on WhatsApp?", asks=True)]},
    {"name": "work: show a ticket, move it", "steps": [
        S("show ACME-12", "show_issue", {"key": "acme-12"}, "ACME-12: the export button is broken. It's in todo.",
          result={"key": "ACME-12", "title": "Export button broken", "status": "todo"}),
        S("move it to done", "move_issue", {"key": "acme-12", "status": "done"}, "Moved ACME-12 to done.")]},
    {"name": "work: list, show the first", "steps": [
        S("show me my open issues", "list_issues|work_summary", {}, "Two open: ACME-7, login times out, and "
          "ACME-9, export to CSV.", result=ISSUES),
        S("show me the first one", "show_issue", {"key": "acme-7"}, "ACME-7: the login page times out.")]},
    {"name": "work: show, then fix it", "steps": [
        S("show ACME-3", "show_issue", {"key": "acme-3"}, "ACME-3: the search box ignores accents.",
          result={"key": "ACME-3", "title": "Search ignores accents"}),
        S("fix it", "fix_ticket", {"key": "acme-3"}, "Shall I start the AI fix for ACME-3?", asks=True)]},
    {"name": "notes, then the server", "steps": [
        S("what did I note about the server", "find_notes|search_my_files", {"query": "server"},
          "You noted the server's disk is 90% full.", result=[{"text": "server disk 90% full"}]),
        S("is it up?", "lab_status", {}, "Yes, calypso is up.")]},
    {"name": "notes: add, then recent", "steps": [
        S("add a note: buy a new router", "add_note", {"text": "router"}, "Noted."),
        S("what were my last notes", "recent_notes|find_notes", {}, "Buy a new router, and the server disk.")]},
    {"name": "shopping: milk, and eggs", "steps": [
        S("add milk to the shopping list", "add_to_list", {"text": "milk"}, "Added milk."),
        S("and eggs", "add_to_list", {"text": "egg"}, "Added eggs too.")]},
    {"name": "apps: open chrome, close it", "steps": [
        S("open chrome", "open_app", {"name": "chrome"}, "Opening Chrome."),
        S("close it", "close_app", {"name": "chrome"}, "Closed Chrome.")]},
    {"name": "phone: timer, make it 15", "steps": [
        S("start a timer for 10 minutes on my phone", "phone_timer", {"minutes": "10"}, "Timer set for 10 minutes."),
        S("make it 15", "phone_timer", {"minutes": "15"}, "Changed it to 15 minutes.")]},
    {"name": "phone: torch on, off", "steps": [
        S("turn on the torch", "phone_torch", {"state": "on"}, "Torch on."),
        S("turn it off", "phone_torch", {"state": "off"}, "Torch off.")]},
    {"name": "browser: search, now for Tokyo", "steps": [
        S("google cheap flights to Bangkok", "search_in_browser", {"query": "bangkok"}, "Searching for that."),
        S("now for Tokyo", "search_in_browser", {"query": "tokyo"}, "Searching Tokyo flights.")]},
    {"name": "reading list: save, then list", "steps": [
        S("save this for later https://example.com/post", "save_for_later", {"url": "example.com"},
          "Saved it."),
        S("what's on my reading list", "reading_list", {}, "One thing: example.com/post.")]},
    {"name": "watching a page: watch, list, stop", "steps": [
        S("watch https://example.com/price for changes", "watch_page", {"url": "example.com"},
          "I'll tell you when it changes."),
        S("what am I watching?", "list_watches", {}, "Just example.com/price."),
        S("stop watching it", "stop_watching", {"name": "example.com"}, "Stopped.")]},
    {"name": "routines: list, run the morning one", "steps": [
        S("what routines do I have", "list_routines", {}, "Two: morning and focus.",
          result=[{"name": "morning"}, {"name": "focus"}]),
        S("run the morning one", "run_routine", {"name": "morning"}, "Running your morning routine.")]},
    {"name": "lab, then the backup", "steps": [
        S("is the laptop server up", "lab_status", {}, "Calypso is up, all services fine."),
        S("what about the backup", "backup_status", {}, "Last night's backup went fine.")]},
    {"name": "repos: today, then argus", "steps": [
        S("any changes in my repos today", "what_changed_today|repo_status", {},
          "Three commits in argus, none elsewhere.", result={"argus": 3}),
        S("what state is argus in", "repo_status", {"name": "argus"}, "Argus is clean, on main.")]},
    {"name": "small talk, a task, small talk", "steps": [
        S("how are you", "chat", {}, "Doing great, just keeping your PC company."),
        S("open spotify", "open_app", {"name": "spotify"}, "Opening Spotify."),
        S("thanks", "chat", {}, "Any time.")]},
    {"name": "money, then a detail", "steps": [
        S("how much did I spend this month", "money_this_month", {}, "About 84,000 rupees so far.",
          result={"total": 84000}),
        S("and how much on food?", "money_this_month|reply", {}, "Food was about 21,000 of it.")]},
    {"name": "web: a question, then a follow-up", "steps": [
        S("who won the cricket yesterday", "web_search|web", {"query": "cricket"},
          "Sri Lanka beat India by 5 wickets.", do={"query": "cricket result yesterday"}),
        S("who was man of the match?", "web_search|web|reply", {}, "Kusal Mendis, 82 not out.")]},
]
