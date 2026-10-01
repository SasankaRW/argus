# Changelog

All notable changes to Argus. Versions follow `MAJOR.MINOR.PATCH`. New entries go under **Unreleased**;
`dev.ps1 release` turns that section into the next version.

## Unreleased

- **"Open Brave"** and similar plain app requests open the app at once, without a model step.
- **Ari feels like one flow:** the screen and clipboard tools are only offered when you ask about them (a weather
  question no longer looks at your screen), and "what's on my screen?" / "what does this error say?" goes straight
  to the screen: no model step first, its answer is the reply. Such questions no longer hit Argus's own "errors"
  rule. While Ari works, the chat bubble says what it's doing ("looking at your screen…").
- **Weather in a second:** "how's the weather?", "will it rain tomorrow in Galle?" are answered at once from
  Open-Meteo (your town from `brief.weather`); the think loop has a `weather` tool too.
- Fix: the MCP endpoint reads the request before refusing a wrong token (Windows reset the connection instead of answering 401).
- **Talk with Ari like a conversation** (`ari.live`, on by default): after "Hey Ari" you just talk back and forth;
  talking over Ari pauses it at once, and it carries on if that was only "mm-hm" or its own echo; "thanks Ari"
  ends it, or `ari.talk_idle_s` of quiet. Knows when you've finished a sentence (Smart Turn v3.2, Silero VAD),
  speaks answers sentence by sentence, and pulses softly while thinking. `ari.live: false` keeps the classic mode.
- Ari, more natural: carry on without "Hey Ari" for a few seconds after an answer; say "stop" or "Hey Ari, …" to interrupt; it offers to remember lasting facts you mention (your yes keeps them); harder questions start on the bigger local model; important messages (overdue Tracker issues, price drops, failed backups, reminders) are said out loud at the PC while you're there, 08:00-22:00, at most one every 10 minutes.
- Everything search (new plugin `everything`): Ari finds any file on the PC instantly through Everything's es.exe; blocked folders never show.
- Helios build updated (the plugin page says "your phone" for phone plugins).
- Tracker and Life Hub for Ari (connector plugins `tracker`, `lifehub`): work summary, list/add/move issues, the timer; the shopping list and wishlist; this month's money (read only, private: never sent to Claude). Adding or moving issues asks first. `permissions.network` can name a setting ("config:url"), so a connector reaches exactly the address you set.
- Argus for Android (`android/`): Helios full screen plus phone tools for Ari (new plugin `phone`: battery and network, ring loudly, Do Not Disturb, torch, timer, open an app or a link). The phone connects to Argus as a worker with the capability `phone` (outgoing only, over Tailscale); a setup page for the address, token and phone permissions. Build once in Android Studio (android/README.md). Plugins can now say `runs_on: phone`.
- New Ari skills: read later (save a page from Ari or the phone's share menu: text + local summary in Documents/read-later, "what did I save about ..."), file tools (merge PDFs, pictures to PDF, pages out of a PDF, smaller copies of pictures; new files beside the originals, Ari asks first), weekly review (Sunday's summary has your week and suggests scheduling what you keep doing by hand; "how was my week?").
- Web for Ari (new plugin `web`): the local models search your own SearXNG (`deploy/searxng`, 127.0.0.1 only) and read pages, so current questions no longer need Claude (it stays the fallback). Outbound only; pages are public websites only (checked again after redirects and on the actual connection); web text is marked untrusted, and after it anything that does something asks you first.
- Island: shortcut chips have small line icons by what they do (a page, a routine, a plugin button, sleep, a website, the phone).
- Helios buttons reworked: one system everywhere. Main actions are dark keys with amber text and a hairline amber edge (no flat yellow slabs), "no"/secondary buttons are quiet outlines, the mic is a round key, your chat bubbles are a warm tint, Dry-run chips are neutral (Live stays green), one focus ring for the keyboard.
- Island, opened: more compact (336 px), the time smaller, Talk is a round green mic key next to a round "···", status rows sit in one quiet card ("idle" instead of "0 running · 0 queued"), shortcut chips are smaller with a hairline edge.
- Island: Ari's answer stays in the same slim pill it spoke from (no switch to a bigger, different-looking box); the shadow is a real soft blur that fades out, smaller, with no hard ends.
- CI: Pillow is a dev dependency (the screen plugin's tests use it).
- Plugins: schedules read in words ("every 3 hours", "weekdays at 09:00"; the cron line is on hover).
- Code review fixes: tools marked `private` (screen, clipboard) keep the chat local, so Claude never sees them, a web search gets only the question, and they aren't offered over MCP. Watchers also check the address they actually connect to, so DNS rebinding can't reach the LAN. The logon supervisor leaves package updates to the boot one. A watch added during a check is kept, and a page read without a price keeps the last price (no repeat alerts). The page reader is linear on broken HTML. The weather is kept for an hour. Replies keep snake_case names.
- Polish pass over every page: the Logs header's source switch no longer loses its border (a Plugins style leaked into it) and the log fills the page; Models shows network errors in plain words and keeps tile headers on one line; the home tile counts machines, not worker threads (a fast lane is its worker, in the queue and status too); the power box says "idle 5 min · sim"; the island's shortcut editor stacks name and action so nothing is cut off; the voice picker fits the phone; Runs shows a tool's own sentence and list counts instead of "…"; settings with text get a proper box; watchers takes shared links from the phone (a price in the note: "under 250,000") and prints Rs amounts cleanly.
- Helios > Settings > **Ari's tools**: a test button that runs each tool that only looks (PC status, apps, clipboard, notes, routines, the lab, repos, watches, file search; "deep" adds looking at the screen) once, as Ari would, and shows which work and why not. Nothing opens, types or changes. Text settings get a wider box.
- Watchers (new plugin `watchers`): "watch this page", "tell me when <product> on <page> is under 280,000"; checked every 3 hours, the phone hears when a page changes or a price drops or goes under yours (once). A product name narrows the watch to that part of the page. `permissions.network: ["*"]` now means public websites only: never this machine, the LAN or the tailnet, also after a redirect.
- Screen and clipboard (new plugin `screen`): "what's on my screen?" (the vision model V1 looks at a screen grab kept in memory, never saved; without V1 the text on screen goes to T1) and "summarise what I copied" (text or a copied picture). Local models only: nothing on the screen or clipboard goes to Claude.
- Morning brief upgrade: today's weather first (`brief.weather`, Open-Meteo, no key; also in Helios > Settings); say "good morning" (or "brief me", "what's my day look like?") and Ari says the brief: weather, what waits for you, what failed overnight, today's first schedule. Ari's own chats no longer count as overnight jobs.
- Laptop move prep: `pc-worker.ps1 check <laptop>` looks before the move (laptop answers, token accepted, the PC's Argus stopped, Ollama models, Fast Startup, the wired card's address); after the move a logon-time supervisor (`--desk-only`) keeps Ari's PC tools, "Hey Ari" and the island on the PC; the map shows the PC's session worker as the same PC.
- Ari is smarter: combines tools across notes, files, routines, the lab and repos in one answer; follow-ups ("open it", "again") see what its tools found last time; retries a search that found nothing; its last step always says what it got done; replies are cleaned to be read aloud (no markdown, links or lists).
- Ari's island is now **native Qt**: no browser engine inside, a few MB of memory, and nothing runs while it's idle. It's drawn by hand with smooth curves at every size (a soft tapered lip when idle) and a clean shadow that is no longer cut off.
- Island, when opened: compact and clean, with the time, **Talk** (the PC's "Hey Ari" listener starts listening now, or Helios's mic when no listener runs), status, inbox, the next job, and your shortcuts on one row. Clicking anywhere else folds it.
- "Hey Ari" and the island run on the PC with your desktop session, also after Argus moves to the laptop (they follow ARGUS_URL). New `POST /ari/wake` and `POST /ari/listener`.
- The browser-based popup page is removed from Helios; the `popup` extra is now PySide6-Essentials, which is much smaller.
- New Argus icon: the iris, a warm-to-blue gradient ring with a bright pupil (app icon, maskable icon, favicon, Helios header). It replaces the sun.
- The phone app: Helios installs as **Argus** (More > install-app). It has a new icon, long-press shortcuts (Talk to Ari with the mic on, Inbox, Share, Map), opens instantly from its own copy, and says "can't reach argus" when the PC or Tailscale is off. The share menu works as before.
- New Argus mark (app icon, maskable icon, favicon, Helios header): a glowing core with a fine corona and an orbit.
- Ari's island: text in Inter (variable weight, optical sizing), lighter and smoother on Windows than Geist at medium weight; a thin 40 px clock.
- Ari's island: the outline is now one smooth SVG shape (curved shoulders into the screen's edge, round bottom corners) moved by a real spring, so it stays smooth at every size, including the slim idle lip.
- Ari's island: clicking it opens your widgets instead of the last chat: the time and date, how Argus is doing, what waits in the inbox, the next scheduled thing, your shortcuts, and optionally Ari's last answer. Shortcuts can be Helios pages, plugin buttons, routines, power or ring-the-phone, or a website.
- Helios > Ari > **ari/island**: turn widgets on or off, reorder them, and add, edit, reorder or remove up to 8 shortcuts. `GET/PUT /island`, `POST /island/run`.
- Ari's island: narrower idle lip (104 px). Spring motion with a slight settle, content fading in with a soft blur, a subtle sheen and a glow in Ari's colour while active, smoother bars and spinner, and a shimmer on the text while thinking.
- Ari's **island** on the PC replaces the popup pill. It's a black shape grown out of the top edge of the screen, and its shoulders curve into the edge. It's always there, as a slim lip when idle and "ari" on hover. It widens while Ari listens, thinks, works or talks (bars, a spinner, and what Ari is on), shows the answer when done, and folds back. Click it for details: the last question and answer, what waits in the inbox, and open chat / new chat / inbox / Helios. Only the island takes clicks; the rest of the window lets them through. Title protocol: `ari:<mode>|<w>x<h>`, `ari:go|<hash>`.
- Guidance: **Run tests now** on a playbook (Plugins > Learning). Its tests are your Correct answers, else the ones the small model got right, and they're replayed on the first local tier with the lessons in use. It keeps a pass-rate history (table `eval_runs`, migration 0013), shows the tests, and lets you remove one. `GET/POST /guidance/{key}/evals`.
- Guidance: **Try with another model** under each model answer (job details). The same question goes to another tier, and both answers are shown side by side with whether they agree. `POST /samples/{id}/replay`.
- Guidance: the nightly review no longer offers lessons that pass fewer tests than the playbook does now ("didn't help"). Its test scores go into the history too.
- Inbox: a lesson's scores show as passed/total.
- Helios **Inbox** (`#inbox`, nav badge): approvals, lessons from the nightly review, and Ari's open questions in one list, newest first. Review, approve or answer each one right there. The "awaiting input" details links on the home tiles open it. `GET /inbox`.
- Helios **Settings** (`#settings`): Argus's own settings (quiet phone, brief and summary times, approval reminders, Claude's daily cap, nightly review, power mode and idle time, backups, Ari's pill) change right away and are kept over argus.yaml. "↺" goes back to the argus.yaml value, and a bad value is refused with the reason. `GET/PUT /argus-settings`.
- Helios **Models** (`#models`): each tier's state, calls per day over the week, Claude's daily cap and login, calls and hand-ups per plugin, and the models pulled in Ollama. `GET /models/usage`, `GET /models/ollama`.
- New plugin **routines**: named chains of Ari's tools ("work mode" = open VS Code and Chrome, volume 20). Ari tools run_routine, list_routines, save_routine and delete_routine (saving or deleting asks you first and shows the steps). Stops at the first step that fails and says which.
- New plugin **notes**: "note: ..." goes into ~/Documents/notes/YYYY-MM-DD.md, time-stamped, only ever added to. Ari tools add_note, find_notes, recent_notes.
- New plugin **homelab**: every hour, disk space, GPU temperature and memory, Ollama models, the newest Argus backup, and Tailscale devices. Problems go to the evening summary, once a day each. Ari tool lab_status.
- New plugin **devhelp**: your repos' branch, uncommitted files, commits not pushed, last commit and last CI run (gh), plus what you changed today. It only reads. Ari tools repo_status and what_changed_today.
- Core: `ctx.files.append_text` (add to the end of a file, never change what's there); built-in tool `backup_status`; `ToolFailed` is exported from argus.worker.
- Ari: your chats are kept and listed (Ari page: newest first, find, delete); open one to carry on, "‹ chats" goes back. Questions from other screens and "Hey Ari" continue the chat you had open. Settings, schedules and memory sit beside the list.
- Ari: messages no longer get squeezed (text spilled out of its bubble); "you" / "ari" labels, terminal-style bubbles, the chat fills the page.
- Ari: files are found by name however it's written (President.Curtis = president curtis = President_Curtis), and "search my files" also returns files whose name matches.
- API: `GET /ari-chats`, `DELETE /ari-chats/{conv}`.
- Helios (PC): every page gets the terminal-widgets look (rounded windows with "● ~/name" titles, mono type, soft glows, stat tiles); the header shows `sas@argus:~/page$`; the inspector opens only when you pick something, so pages use the full width.
- Helios: the home "tail" tile is now a live log view (~/logs); the "saved" tile and stat are gone; the tile strip under the map fills the width; map "new" badges only for parts added in the last day.
- Map: one box for the PC: the worker's fast lane and "pc" in power events are drawn as the PC's worker.
- Fix: the header's ask box no longer picks up the phone prompt's style (the stray amber capsule).
- Helios (PC): two home layouts, switched top right and remembered per browser: "desk" (the live map as the main focus with terminal tiles under it: Ari prompt, awaiting input, jobs, saved, PC, tail) and "command" (the map fills the screen, cards in the corners, one command bar: ask Ari or type /queue, /plugins…; Ctrl+K or / to jump to it). The map zooms in further to fill its space.
- Ari: the listener names the microphone it uses in logs\ari.log.
- Ari: "Hey Ari" on the PC no longer freezes while starting: once Whisper can't use the GPU, every model goes straight to the CPU (a second GPU try hung), and the GPU check gives up after 30 s.
- Ari: pick Ari's voice (11 English Piper voices, downloaded the first time) and speed in Helios (Ari page); kept by Argus so "Hey Ari" on the PC uses it too. `GET /ari-voice/voices`, `PUT /ari-voice/voice`.
- Ari: recordings from the browser decode with newer PyAV (fixes `open() got an unexpected keyword argument 'metadata_errors'`).
- Ari: the PC listener logs its microphone, the loudest level each minute, and what it heard without the wake phrase (to find why "Hey Ari" misses).
- Ari never waits in the queue: each worker runs a fast lane (a second loop, `worker-<name>-now`) that takes only interactive jobs (Ari's thinking and the tools it uses, Ask Argus), so they start at once even while the main lane runs a long job. Interactive jobs also skip the one-GPU-job-at-a-time rule. `argus-worker --no-fast-lane` turns it off.
- `trigger.duplicate` events are gone: a file the folder watcher already reported (after a restart, or renamed by a plugin such as screenshot-renamer) is recognised quietly by its content, and its new path is remembered.
- Phone: rebuilt from scratch as "terminal widgets". A `sas@argus:~$` header with an ONLINE badge, soft green and amber glows, and tiles that are small terminal windows: `~/ari` (Ari's last reply, an `› ask ari…` prompt and a mic button that opens Ari listening), `! awaiting input` (approve or reject right there, details in a bottom sheet), `~/jobs` today, `~/saved`, `~/running`, `~/pc` and a live `~/tail -f`. A floating pill tab bar (home, ari, plug, map, more), job details in a bottom sheet, plugins as a list with `cd ..` back, More as a directory listing.
- The full dashboard on a phone has a "phone view" button to get back.
- The folder watcher no longer floods the event log with `trigger.duplicate` when it re-reports the same files after a restart (a copy of a file somewhere else still shows).
- Fix: Ari's replies never arrived (and so were never spoken) on a database that applied migration 0011 before `ari_turns.used` was added to it ("no such column: used"). Argus now adds such late columns when it opens the database.
- Fix: "Hey Ari" failed on every clip when Whisper loaded on the GPU but CUDA's cuBLAS was missing ("cublas64_12.dll is not found"). Whisper now proves the GPU works on a moment of silence when it loads, and uses the CPU otherwise.
- Ari's popup sits a little lower so its glow isn't cut off at the top of the screen.
- The detailed architecture diagram redrawn as a clean C4-style container view: light theme, four clearly separated zones (clients, argusd, PC worker host, external services), argusd split into client API / services / worker API / engine / storage, straight edge-to-edge connections with 11 numbered flows and a legend.
- docs/diagrams/architecture-detailed.svg: the detailed architecture diagram (clients, every part of argusd by layer, the PC worker, plugins, models, Ari on the PC, external services, twelve numbered flows), linked from docs/architecture.md with a table of the flows.
- docs/architecture.md: the project explained. The big picture with a diagram, the life of one job, where things live, and each part of the core in its own expandable section (argusd, the store, jobs, events, workers, plugins, models, approvals, scheduler, Ari, guidance, Helios, MCP, security).
- Ari pill: solid background, and a wider, softer glow around the edge (both looks).
- Ari pill, smooth redesign in two separate looks (pick one on the Ari page; Argus's default is `ari.pill: pulse`): a smooth black capsule whose edge is lit by a soft blurred light instead of a hard line. **Pulse**: the light breathes with the voice while Ari listens or speaks, slowly while it thinks. **Comet**: a soft light travels round the edge, faster while Ari works. Soft orb with sound bars or dots, blur-in text, and the capsule sizes itself to its content and glides between sizes. `/ari-voice` now also says the default look (`pill`); the PC popup follows it.
- Ari pill, premium redesign (pulse + comet): a black glass capsule with two lights on its edge. A ring that pulses with the voice while Ari listens or speaks, and a comet that runs round the edge while Ari thinks or works (faster while working), with a soft glow beneath in the state's colour. A glossy orb shows sound bars or a spinning arc. The pill sizes itself to its content and glides between sizes; long answers take two lines. Works at any size (the lights follow the capsule's shape).
- The PC popup no longer keeps showing "speaking" when the answer arrives from another screen.
- Ari pill: a glowing capsule at the top of Helios while Ari listens, thinks, works (shows the tool: "searching your files", "searching the web") or talks, then shows the answer for a moment and folds away. Terminal colours, turning gradient edge, sound bars, a braille spinner; tap it to open Ari, × stops the voice. It follows Ari from any screen through the new `ari.state` event (`POST /ari/state`; Argus reports thinking and the answer itself; `ari_listen` reports listening and speaking).
- Ari's popup over the whole PC screen: `python -m argus.ari_popup` (`pip install -e .[popup]`, PySide6; `ari.popup: true` makes `dev.ps1 up` start it). A see-through always-on-top window at the top middle of the screen showing the same pill over any app, never taking the keyboard; it hides while Ari is idle. Helios in a browser on that PC then leaves the pill to it (`/ari-voice` says `popup_here`).
- Phone: a "Live map" button opens the map as its own full-screen page, turned sideways so the left-to-right map uses the phone's long side (turn the phone to read it); tap a box for its details, × or Esc closes. On Android it also goes full screen and holds landscape. The full dashboard on a narrow screen gets a "full screen" button on the map.
- Map lines are now drawn from the boxes' known handle spots instead of screen measurements (needed for the turned map; long lines follow the layout's routes).
- Live map: shows the main parts only. All plugins are one "Plugins" box (count, and what runs or waits); clicking it opens the Plugins page. Lines and message dots to any plugin go to that box.
- Helios redesign: a modern terminal look. Status line on top (health, uptime, Claude calls, clock) with an `› ask argus…` prompt; an icon rail that expands to labels (remembered); the live map fills the page with the job counts and legend floating over it and the inspector sliding in from the right; events are a `tail -f` pane docked at the bottom that folds to one line; job counts are one mono strip on Queue, Runs and Power.
- Smooth motion: pages and panels rise in, the inspector slides in, the events pane opens, buttons press, changed numbers flash, busy boxes glow; all off when the system asks for reduced motion.
- Phone: the "Talk to Ari" button text was invisible (dark on dark); fixed.
- **Guidance loop:** every model answer a plugin gets is kept (playbook, input, answer, tier, escalated). In a job's page you mark answers Correct (they become that playbook's tests) or Wrong (with what it should have been). Nightly at `guidance.at` (03:30), or "Review mistakes now", Claude turns new mistakes into short lessons; the tests are replayed on the local tier with and without them; you approve on the phone or in Helios, and approved lessons are added to that playbook for that plugin from the next job on. Migration 0012; `/guidance`, `/samples/{id}/verdict`, `/lessons/{id}/decide`.
- **Helios Plugins page:** every plugin in one clean list (grouped: Files, PC control, Other; live or dry-run at a glance, search), and one page per plugin with its own controls: Dry-run/Live switch, its buttons, and tabs for Overview (when it runs, what Ari can ask it, what it may touch), Settings (a form from its plugin.yaml, saved in Argus, reset per field), Runs and Learning (answers, lessons, tests). Changes from Helios are kept across restarts (`PUT /plugins/{id}/settings`). The unfinished "soon" menu entries are gone.
- **New plugin pc-keys:** Ari types text into the window in front (Unicode, any language) or presses a shortcut ("ctrl+s", "alt+tab", "f5"); always asks first, and refuses anything that looks like a password, PIN or key.
- **Ari shows what it used:** under each answer in Helios, the tools it used (and web search), a failed one in red.
- **Ari remembers what you tell it:** "remember that my car service is due in December", "note my laptop's IP is …". Every question Ari thinks about gets the matching facts first (`you_remember`), and it has tools to remember, recall and forget. The Ari page lists "What I remember" with Forget buttons; `GET /ari-memory`, `DELETE /ari-memory/{id}`. Migration 0011. Only what you ask it to keep.
- **New plugins pc-windows and pc-system** (Ari's tools, your Windows session): switch to a window, show the desktop, lock the PC, take a screenshot (Pictures/Screenshots, then named by screenshot-renamer); how the PC is doing (CPU, memory, disks, GPU use and temperature via nvidia-smi, the busiest programs), read the clipboard, copy text to it (asks first). psutil added to `.[plugins]`.
- **New plugin knowledge ("My files", for Ari):** a local index of your documents, notes and projects (text, Markdown, code, Word, PDF; Documents, Desktop, Notes, G:\Projects), and file names only for Downloads, Pictures, Videos, Music. Nightly at 2:15 or "Index my files now"; only changed files are read again; skips node_modules, .git, venvs and build folders. Search by words (SQLite FTS5) and by meaning (vectors from `nomic-embed-text` via Ollama, when pulled), combined. Ari's tools `search_my_files` and `find_file`. The index stays on the PC (data/plugins/knowledge).
- For plugins: `ctx.data_dir` (a private folder on the worker's machine), `ctx.embed(texts)` (local embedding model), `ctx.files.walk(..., skip_dirs=)`. Extras `.[plugins]` now include pypdf and numpy.
- **Ari thinks with tools** (job `ari.think`): anything the instant rules can't answer goes to the local models, which pick tools one step at a time (up to 6) and then answer. Your own things first (Argus's data, plugins' tools), general knowledge from the model, current things (`need_web`) from Claude with web search only (`--allowedTools WebSearch,WebFetch`). Tools that change things ("asks first": run a button, close an app) wait for your yes.
- **Tools framework:** plugins declare tools in plugin.yaml (`ari: tools:`); built-in tools for Argus's data and the phone (shared with the MCP server). `GET /tools`; `ctx.tool(name, args)` for workflows (a plugin tool runs as a child job; the caller waits and is resumed when it ends).
- **New plugins pc-apps and pc-media** (need the `session` capability: your logged-in Windows session): open apps by name (Start menu and Store apps, loose matching), files, folders and websites; close apps (asks first); volume level/up/down/mute; play/pause, next, previous. `dev.ps1 up`'s worker has `session`; `pc-worker.ps1 install` adds an "Argus desktop" task at logon for after the move.
- **"Hey Ari" on the PC, no browser** (`ari.listen: true`, extras `.[listen]`): `python -m argus.ari_listen` (started by `dev.ps1 up`, log `logs\ari.log`) listens on the microphone, cuts speech out of the room's background, checks short clips for the wake phrase with a small Whisper model on the GPU (`ari.listen_wake_model`, tiny.en; also "Hey Harry", "OK Ari"), takes the command in the same breath or after a chime, sends it to Ari (same conversation as Helios) and speaks the answer with the Piper voice. After "Shall I?" a plain yes/no is enough. Nothing leaves the PC; what the mic hears while Ari speaks is ignored.
- **Argus as an MCP server** (`POST /mcp`, Streamable HTTP with JSON answers, the Helios token): Claude Code can see Argus's status, jobs and their steps, logs, waiting approvals, schedules and time saved, list and run plugin buttons, and ask Argus. It can't approve, power the PC or touch files. `.\scripts\dev.ps1 mcp` adds it to the claude CLI. Checked with Claude Code itself.
- **Quiet by default** (`ntfy.quiet`, on): a plugin's ordinary messages (`ctx.notify` with priority min/low/default) no longer buzz the phone; they come together in the evening summary ("Also: …"). Approvals, failures, reminders, warnings and high/urgent messages still come at once. Migration 0010.
- **Find my phone:** "where's my phone?" to Ari or Ask Argus says where Tailscale sees it (online at home on the same Wi-Fi, online away, or offline and when last seen) and offers to ring it: three urgent notifications 20 s apart. Helios > Power > Your phone; `GET /phone`, `POST /phone/ring`.
- **Claude usage meter and login check:** Helios's top bar shows Claude calls used today of the daily cap (and how many are left when few are). Every half hour each worker runs `claude auth status` (free, no call) and tells argusd; a failed call that asks for a login counts too. When Claude is logged out, the phone hears it once (with what to do) and Helios shows "Claude logged out". `POST /workers/{id}/claude`; `GET /models` has `claude.logged_in`.
- **Time saved:** plugins estimate what each job saved you (`ctx.saved(seconds)`; downloads-organizer ~20 s per file, screenshot-renamer ~30 s per name, duplicate-finder a minute plus ~10 s per file). Helios shows this week's total as a "Saved you" tile and a card on the phone; `GET /time-saved?days=7`. Counted once per job even when retried. Migration 0009.
- **Evening summary** at `summary.at` (20:00, a quiet notification): today's jobs by plugin, the time they saved, what failed or waits; on Sundays the week's total. `POST /summary` sends one now.
- **New plugin: duplicate-finder.** Finds files with identical content (same size, first 64 KB, then full SHA-256; names don't matter) in the folders you pick (Downloads and Pictures by default, compared together), keeps the best copy (outside Downloads, plain name, shallowest, oldest) and, after one batch approval, sends the extras to the Recycle Bin, checking each again right before. Sunday 4 am or "Find duplicates". Dry-run until listed in `plugins.live`.
- `ctx.files.walk(folder)` (every file below a folder) and `ctx.files.sha256(path, limit=)` (read in pieces) for plugins.
- Tests: the approve-from-the-phone timing check allows 6 s on shared CI runners (1 s locally).
- Fixed (Windows): stopping argusd could fail with "file in use" when the running-marker was being refreshed at the same moment.
- Tests: the nightly backup and the morning brief no longer start on their own during tests (they depend on the clock and made CI fail after 02:30 UTC).
- **Fix: Ari's mic and "Hey Ari".** The mic button and the "Hey Ari" listener no longer fight over the microphone (the listener steps aside while you use the mic). When hearing fails, Ari now says why (microphone blocked, no microphone, the browser's speech service unreachable, nothing heard) instead of doing nothing, and "Hey Ari" turns itself off with the reason after repeated failures. The wake phrase is found anywhere in what was heard and in its usual mis-hearings ("Hey Harry", "OK Ari", "Hey, Ari."). With `ari.hearing: whisper`, "Hey Ari" also runs on Whisper on the PC (private, any browser, no Google).
- **Morning brief:** at `brief.at` (07:00) the phone gets one message: overnight results and failures, what waits for you, today's schedules, the backup, the PC, Docker, yesterday's Claude calls. `POST /brief` sends one now.
- **"Argus is back" after a power cut or crash:** argusd keeps a marker while it runs; if the last run ended abruptly, the phone hears when it stopped, which jobs resume from their last finished step and which missed schedules run now. A stop by `dev.ps1 down`, the supervisor or systemd is not reported.
- **PC health (WSL and Docker):** `health.enabled` makes the PC's worker check WSL distributions and Docker containers every `health.every_minutes` (never waking the PC for it) and tell the phone only when something changes: a container in `health.containers` stopped or is missing, one reports unhealthy, Docker stopped answering, and when it is fine again.
- **Ari's natural voice and hearing (optional):** `ari.voice` names a Piper voice: Ari speaks with it (`POST /ari-voice/say`, where argusd runs) instead of the browser's voice. `ari.hearing: whisper`: the mic button records until you stop talking and Whisper on the PC writes it down (GPU when CUDA is there, else CPU; the browser hears you while the PC is off). Extras `.[voice]` and `.[hearing]`; setup in docs/setup.md > Ari.
- **Ari:** talk to Argus in Helios (new Ari page; "Talk to Ari" on the phone), by typing or speaking. Ari answers aloud if you like, keeps the conversation (a model sees the last turns), and asks before doing anything: say or tap Yes. "Hey Ari" wake phrase while Helios is open.
- **Schedules by voice:** "sort downloads every morning at 7", "every weekday at 9 …", "remind me to call mum tomorrow at 5 pm" (phone reminder), "in 20 minutes …", "shut down the PC at 11 pm": Ari repeats what it understood and makes the schedule after your yes. Your schedules are listed on the Ari page (pause, delete); `PATCH`/`DELETE /schedules/{id}` for yours; schedules from argus.yaml and plugins are untouched. Migration 0008.
- **Claude for every plugin when the local models can't:** when every local tier fails, `ctx.llm` gives Claude one try with the same input, the pictures and the rejected answers, within the plugin's daily Claude calls (`claude.plugin_calls_per_day`, 10, unless its plugin.yaml sets `claude_calls_per_day`; 0 = never). Claude now sees pictures too (the CLI's stream-json input). New `ctx.ask_me` for the last resort: you fill in the answer in Helios or on the phone, with a picture to decide by.
- **screenshot-renamer:** vision, then the text on screen, then Claude looking at the picture; if nobody can name it, Helios and the phone show the screenshot and ask you ("Name this screenshot"; Reject leaves it). **downloads-organizer:** Claude before the by-type fallback.
- **Review fixes (safety):** automatic shutdown now reads who is signed in with `quser` (works for the boot-time worker) and never shuts down when it can't tell; Cancel also drops a queued automatic shutdown; power buttons answer "the PC is off" instead of queueing (a queued shutdown no longer wakes the PC, and a power job older than 5 minutes for an offline PC is dropped).
- **Review fixes (data):** a failed backup is reported once per night instead of every few seconds, and a half-written backup is never counted; parallel uploads to one share no longer corrupt it (same-name files, names differing only in case, Windows device names); shared HTML/SVG downloads instead of opening in Helios; Undo and Wrong on the same change can't both run.
- **Review fixes (plugins):** a file's own name is taken literally (a `$HOME` in a download's name no longer breaks it); "never overwrite" is now atomic and each alternative name is checked against the allowed folders; a change is recorded after it happened, and kept and sent later if argusd was restarting; the dry-run Tidy preview only shows a folder removed when every file in it would move; files shared from the phone are saved one step each (a retry doesn't save one twice).
- **Review fixes (processes):** the supervisor no longer leaks log handles or exits when a package update fails; the worker keeps running when argusd refuses a claim or a start.
- **Laptop deploy kit:** `deploy/linux/install.sh` (Ubuntu: argus user, /opt/argus, venv, services at boot, firewall: only Tailscale and SSH, automatic updates, tailscale serve), systemd units, `argus-update.sh` (backup, pull, check, restart, roll back if unhealthy), a laptop config template, and `scripts/pc-worker.ps1` (the PC as the GPU worker and Ollama at boot, before login). Guide: `deploy/README.md`.
- **Supervisor:** `dev.ps1 up` now runs argusd and the worker under `argus.supervisor`, which restarts them if they stop and after new code is pulled (once no job runs; updates packages when pyproject changes).
- **Nightly backups:** at `backup.at` (02:30), checked by restoring each one, newest `backup.keep` kept; the PC worker copies each to `backup.copy_to`; a failed backup notifies the phone. `GET/POST /backups`.
- **Real automatic power** (`power.mode: real`, on the laptop): Wake-on-LAN when PC work waits; after `idle_minutes` a phone warning, then shutdown after `warn_minutes` unless cancelled. Only for a PC that Argus woke (unless `shutdown_manual_sessions`), and the PC skips it while someone uses the keyboard or mouse.
- Fixed a rare crash at shutdown (seen as a segfault in CI): the database now waits for reads in progress before closing its connections, and refuses new ones.
- **Ask Argus:** a box at the top of Helios (Ctrl+K) and on the phone, with a microphone button where the browser supports it. Common asks ("sort downloads", "what's running?", "anything waiting for me?", "shut down the PC", "open logs") are answered at once by rules; the rest go to T1/T2 as a job that answers from a snapshot of Argus. It suggests at most one action, which runs only when you tap Do it. `POST /ask`, `POST /ask/do`.
- **PC power buttons:** Helios > Power and a "PC power" card on the phone: Sleep, Restart, Shut down (after `power.shutdown_delay_seconds`, with Cancel) run on the PC's worker ahead of other jobs; Wake sends Wake-on-LAN from argusd (`power.pc_mac`). Shows whether the PC is on and what auto power would do. `GET /power`, `POST /power/{wake|sleep|restart|shutdown|cancel}`.
- Helios shows the server's reason when an action is refused.
- **Helios Queue page:** what runs now (worker, step, time), what waits for you, and what runs next in order, each with the reason it waits (after the running job of the same plugin, GPU busy, night window opens at, no worker online, retry in); Cancel on each queued job. `GET /queue`.
- **Simple phone view:** on a phone Helios opens to one clean screen: status, a big "Send something to Argus" button, approvals that need you, Now, Up next and Just finished. Tap any job for details; "Open the full dashboard" switches to the full view (remembered).
- downloads-organizer: videos always go to Downloads\Videos and audio to Downloads\Audio, decided by file type in code (rules `by_extension:`), no model; Tidy folders also moves such files out of the wrong folders. Optional `destinations:` keeps a category outside Downloads.
- **screenshot-renamer names the window in front, not the background:** the vision model goes first (it sees which window the screenshot is about), with the text near the middle as a hint. Every word of a name must be on screen in the front window or be a plain describing word (folder, error, login page); made-up words or words from other windows are sent back, and if nothing passes the name stays as it is.
- **downloads-organizer: one folder per kind.** Media is now Audio; "Tidy folders" folds Pictures into Images, Media and Music into Audio, Miscellaneous into Misc (each move can be undone).
- **Rules in Helios:** plugins with a rules file (downloads-organizer) get an "Edit sorting rules" button: a YAML editor with Save, Reset to default, and the examples learned from Wrong folder. Edits apply from the next job, no restart; rules that make no sense stop the job with a message that says what to fix.
- **Share to Argus:** Helios installs on the phone (Add to Home screen) and appears in its share menu. Share a photo, PDF, link or text, pick where it goes, Send. Also on the new Share page in Helios (Add files).
- downloads-organizer takes shares ("Save to PC"): files are saved to Downloads and sorted at once; links become .url shortcuts, text a .txt.
- Plugins declare `share:` targets; jobs read shared files with `ctx.shared(name)`. API: `GET /share/targets`, `POST /shares`, `PUT /shares/{id}/files`, `POST /shares/{id}/send`; shares are kept 7 days (`share.keep_days`, `share.max_mb`).
- **`dev.ps1 up` without extra windows:** Ollama, argusd and the worker run in the background and "up" prints one tidy summary. `dev.ps1 status` shows what runs; `dev.ps1 logs` follows every log in one coloured terminal. If something fails to start, its last log lines are shown right away.
- **Helios Logs page:** argusd, worker and Ollama logs in one live view, with source tabs, Warnings / Errors filters, search and pause (`GET /logs`, `GET /logs/{name}`).
- The worker can write a rotated log file (`--log-file`); a process running in the background logs only to its file.
- **screenshot-renamer plugin:** new screenshots get names like "2026-09-28 cashly login bug.png". Text on screen (Tesseract) goes to T1; otherwise the vision model V1 (qwen2.5vl:7b) looks at the picture. Names are checked in code; only default names are touched; Undo in Helios.
- Models: `ctx.llm(..., images=[...])` sends pictures to a vision tier; tiers outside the chain (like V1) can be listed in a manifest.
- New `plugins` extra (send2trash, Pillow, pytesseract), installed by dev.ps1.
## 0.7.0 (2026-09-29)

- **Helios Runs page:** every job, newest first, filtered by plugin and state, with a one-line summary; click one for its details.
- **Undo and Wrong buttons:** a job's Changes list shows each file it moved. Undo puts one back; Wrong folder moves it to the folder you pick and keeps that as an example the models see next time (downloads-organizer).
- `GET /jobs?plugin=&before=`; `GET /jobs/{id}/changes`, `POST /jobs/{id}/changes/{event}/undo` and `/wrong`.
- A plugin switched to live (or with new settings) takes effect on the next job, even if the worker registered before argusd restarted.
- downloads-organizer: a dry-run result now says "would_move" and "nothing was moved" instead of "moved".
- Plugin trace events no longer draw stray "file" and "plugin" boxes on the map (old ones are removed).
- **First plugin: downloads-organizer.** Sorts new files in Downloads into category folders: siblings stay
  together, a matching existing folder wins without a model, T1 picks the category (T2 when T1's answer isn't a
  real category), extension fallback last. Starts from the folder watch, a nightly sweep, or its Sort now button.
  Dry-run until listed under `plugins.live`. Rules in `plugins/downloads-organizer/rules.yaml`.
- Helios: a plugin's box shows dry-run or live, what starts it, and its buttons.
- Folder triggers accept `~` in paths.
- **Plugins as folders (C10):** a plugin is `plugins/<id>/plugin.yaml` + `plugin.py`. argusd checks manifests
  (bad ones are listed under `GET /plugins`, never fatal), wires their triggers (schedules, folder watches,
  webhooks, Run now) and applies a per-plugin Claude cap. Workers load the plugins they can run (`--cap desktop
  --cap gpu`, or `ARGUS_WORKER_CAPS`); each job's `ctx` only reaches the manifest's folders, hosts, secrets and
  model tiers (`PermissionDenied` otherwise). New plugins run in dry-run until listed under `plugins.live`; every
  file change is an event; moves never overwrite; deletes go to the Recycle Bin.
- **Power manager (simulated):** logs `power.would_wake` when GPU/desktop work waits with no PC worker, and
  `power.would_shutdown` after `power.idle_minutes` (20) without PC work. Its state shows on the Helios map.
- Fixed a rare hang on shutdown in the event stream and outbox (Python 3.11 `wait_for` cancel race).
- **Map lines are curves again,** routed around the boxes (ELK splines) instead of right angles.
- **Review fixes (29 Sep):**
  - Security: `.env` saved with a BOM (Notepad) no longer turns auth off; an empty `ARGUS_WORKER_TOKEN` counts as
    unset, and argusd refuses to listen beyond localhost without one; secrets are hidden from config printouts;
    tokens in URLs are masked in logs; `/lite` escapes event text; the phone page only links to http(s) and sends a
    strict Content-Security-Policy; approval links must be http(s).
  - Jobs: a worker is never starved by a pile of jobs it cannot run (no top-200 cut; plugin filter in SQL);
    waiting for an approval no longer uses up a retry attempt; Re-run with an active duplicate answers 409 instead
    of 500; cancelling or dead-lettering a job closes its pending approvals; idle long-polls only read.
  - Durability: SQLite commits with `synchronous=FULL` (the laptop has no battery); writes queued during shutdown
    fail cleanly instead of hanging.
  - Models: Claude gets its playbook through stdin, never the Windows command line; a Claude timeout ends the whole
    process tree (claude.cmd -> node) so a worker can't hang; every call needs its own permit (retries count
    against the Claude cap); a half-open breaker lets exactly one trial call through.
  - Worker: an argusd outage while reporting no longer stops the worker.
  - Approvals: edited fields keep their type and an edited amount must be valid (formatted in code); a late
    answer counts as expired and sticks; ntfy mode can carry a write-only token (`NTFY_REPLY_WRITE_TOKEN`).
  - Helios: a changed token shows the login instead of "Connecting" forever; line counts are no longer counted
    twice after a refresh; the map re-renders only while something glows (idle costs nothing); on a phone one
    finger scrolls the page and taps don't drag boxes; the inspector scrolls into view on narrow screens; line
    history looks further back; dev-server proxy covers /models, /approvals, /outbox.
  - Tooling: `up`/`down` check pid and start time (Windows reuses pids) and record each start at once; `merge`
    refuses when local commits aren't on GitHub and waits for checks to appear; CI fails when `helios_dist` is
    stale; `release` prints recovery steps if the push fails; README and CONTRIBUTING match the workflow.
  - Indexes on events by component and jobs by date (migration 0005); `/status` cached for 2 s.
  - 188 tests.
- **Plugin Guide 0.5:** the plugin plan in five waves (24 plugins).

Core step C9: scheduler, triggers and dispatcher.

- **Schedules:** cron jobs under `schedules:` in argus.yaml (`0 7 * * *`, `@daily`; local time). After downtime a
  schedule runs once, not once per missed slot; a run still queued is merged with the next. Helios' Scheduler box
  lists them with the next run and a Run now button (`GET /schedules`, `POST /schedules/{id}/run`).
- **Windows:** `windows: {night: "01:00-06:00"}`; a job or schedule with `window: night` only starts inside it.
- **Folder triggers:** `triggers.folders` names a folder on a worker's machine; that worker watches it and reports
  each file once it has stopped changing (`settle_seconds`), skipping temporary downloads. Files are remembered by
  content, so copies, re-downloads and restarts never process a file twice; when the plugin's queue is full the
  watcher holds the rest back and offers them later (`POST /triggers/file`).
- **Webhooks:** `POST /hooks/<name>`, signed with the hook's secret from .env: a timestamped HMAC (replays refused)
  or GitHub's `X-Hub-Signature-256`. Retried deliveries are merged.
- **Dispatcher:** priorities (interactive 90, resumed after approval 80, scheduled 50, batch 20); one job per plugin
  at a time by default (`jobs.plugin_concurrency`, per-plugin `jobs.concurrency`); one GPU job at a time, and GPU
  jobs for the model already loaded go first, so Ollama swaps models rarely (`model` on a job or schedule).
- Database migration 0006. Core Design 0.8. 201 tests, including the gate: 100 mixed jobs with at most 2 model
  swaps, and a flood of 500 files merged and limited.
- **Devices in their own column:** the PC's worker, the phone and apps always sit in the first column of the
  Helios map under a plain "Devices" heading; the rest is laid out by traffic as before, models last. Lines follow
  ELK's routes around the boxes instead of cutting across them, and the map is
  more compact. A box you drag gets a plain curve until Auto layout.
- **Phone on the map:** a Phone box shows online / offline (green / red) and since when, like the PC's worker.
- **Approval notifications say "Approve / Reject need Tailscale"** when the buttons go over Tailscale.
- **Phone buttons go over Tailscale, and Argus catches up when the phone reconnects:** Approve / Reject go
  straight to Argus at `approvals.public_url`, so knowing the ntfy topic is not enough to approve anything. Set
  `approvals.phone` to the phone's Tailscale name and Argus checks `tailscale status` every 30 s; when the phone
  comes back online and something is still waiting, it sends one message (the card again with fresh buttons,
  or "N approvals waiting"), at most once per 15 minutes. `/health` shows it under `phone`.
- **Optional ntfy mode** (`approvals.buttons: ntfy`, for your own ntfy server behind a login): the buttons post
  to a private reply topic on the ntfy server so they work anywhere; Argus keeps one streaming connection to it,
  decides with the signed one-time token and replies "Approved: ..." (or "Already approved: ...").

## 0.6.0 (2026-09-28)

Core step C8: approvals, the outbox and ntfy.

- **`ctx.approve()` for plugins:** a step asks you and the job waits without holding a worker. When you answer,
  the job goes back to the queue ahead of scheduled work, the step runs again and `ctx.approve` returns your
  `Decision` (truthy when approved; `.fields` has the values as approved, edits included). Three card types:
  `entry` (editable fields), `batch` (items; Argus adds up the count and total in code) and `draft`. Asking twice
  from a retried step returns the same approval, never a second one.
- **Approve from the phone:** the ntfy message has Approve / Reject / Open buttons carrying a signed one-time
  token (`approvals.public_url` must be the address your phone reaches Argus on). Open shows a small page with
  the card. A used or forged link does nothing.
- **Reminder and expiry:** one reminder after 24 hours; after 7 days the approval counts as "no" and the job
  carries on.
- **Outbox:** messages are written in the same transaction as the change that causes them and sent by a
  background task, with retries and backoff while ntfy is down. Nothing is lost when argusd restarts, and a
  message is never queued twice.
- **ntfy:** approvals, dead jobs and `ctx.notify()` reach your phone. Set `NTFY_TOPIC` in `.env`.
- **Helios:** Approvals and ntfy boxes on the map; Approve / Reject (with edits) in the job and Approvals panels;
  an Approvals filter in the event list; plugin boxes show waiting jobs.
- **API:** `POST /jobs/{id}/approvals`, `POST /jobs/{id}/notify`, `GET /approvals`, `POST /approvals/{id}/decide`,
  `GET /a/{id}` (phone page), `GET /outbox`, `POST /outbox/test`. Database migration 0004.
- **dev.ps1:** `ntfy` (test message), `approval` (a pretend bill that waits for you), `approve` / `approve no`.
- **Docs:** Core Design 0.7, Plugin Guide 0.4.
- 173 tests (16 new, including the gate: phone Approve to finished job in under 1 s, and no duplicate
  notification after a worker or argusd crash).

## 0.5.2 (2026-09-28)

- **One command to start everything:** `dev.ps1 up` (or double-click `scripts\up.cmd`) starts Ollama, argusd
  and a worker if they are not running, waits until each is ready, and opens Helios. `dev.ps1 down` (or
  `scripts\down.cmd`) stops what `up` started; `down all` stops Ollama too.

## 0.5.1 (2026-09-28)

- **Helios updates show up on reload:** `index.html` is now served with `Cache-Control: no-cache` (hashed assets
  stay cached), so a browser no longer keeps showing an old Helios after an upgrade.
- **Model check ignores case:** `dev.ps1 models` finds `Qwen2.5:latest` when the config says `qwen2.5:latest`,
  as Ollama does.

## 0.5.0 (2026-09-28)

Core step C7: models, tiers and escalation.

- **`ctx.llm()` for plugins:** ask the cheapest model tier that can do the job. Every answer is checked in
  code (JSON parsed and validated against a Pydantic schema, then the plugin's own check). A rejected answer
  is retried once with the reason, then escalated T1 -> T2 -> T3, and the next tier is told what the smaller
  model said and why it was rejected. `ctx.claude()` asks Claude directly. The step records which tier
  answered, and a checkpointed step never asks again after a crash.
- **Ollama provider:** `/api/chat` with structured JSON output, temperature 0, `keep_alive` so the model stays
  loaded, and a hard timeout (a hung model never hangs a worker).
- **Claude provider:** the `claude` CLI in print mode with every tool removed (`--disallowedTools "*"`,
  one turn, no saved session): text in, text out. Daily cap (default 30 calls) enforced across all workers.
- **Circuit breakers** per tier, kept in argusd: 3 failed calls in a row pause a model for 60 s, then one
  trial call; jobs move on to the next tier instead of piling up. Wrong answers don't count as failures.
- **Helios:** the model tiers are boxes stacked in one column (T1, T2, T3) showing ready / paused and call
  counts; escalations draw an amber line from tier to tier; a paused model turns red; replies travel back
  along the request's line. Click a model for its state, calls, last error and Claude budget. New Models
  filter in the events list.
- **Tools:** `dev.ps1 models` checks Ollama, pulled models and Claude (with a real test call);
  `dev.ps1 classify` runs a demo model job with T1 rejected on purpose, to watch an escalation.
- **API:** `GET /models`, `POST /models/{tier}/permit`, `POST /models/{tier}/report`, `POST /jobs/{id}/events`
  (a worker's trace events); worker registration now hands out the model configuration. Migration 0003.
- **Config:** new `models` and `claude` sections, and `ollama.timeout_seconds` / `ollama.keep_alive`.
- **Tests:** 157, with a fake Ollama (garbage, hang, errors, missing model) and a fake `claude`.

- **Version control:** GitHub connection (`dev.ps1 github`), branch and pull request workflow
  (`branch`, `pr`, `merge`, `sync`), one-command releases (`dev.ps1 release patch|minor|major`), a
  pre-push hook that protects `main`, consistent line endings, and the design docs exported into `docs/`.

## 0.4.0 (2026-09-28)

Core step C6: Helios v0, the live map.

- **Helios** at http://127.0.0.1:8600 (`/` now opens it; the small page moved to `/lite`). Built with
  React, React Flow and ELK in the Helios mockup style (dark, Geist, amber accent). Argus serves the built
  copy, so no Node.js is needed to use it.
- **Live map:** every component is a box, every pair that has talked is a line. A dot travels along a line
  for each message (red for failures), lines get thicker with traffic, boxes light up while busy, new boxes
  fade in with a "new" badge. Workers show online/offline; plugins show running and queued jobs. The map
  lays itself out left to right and keeps everything in view as it grows; drag boxes to arrange them
  (remembered in the browser), Auto layout resets.
- **Inspector:** click a box (details, lines, recent activity), a line (message count, recent messages) or
  any event (the job: state, attempts, steps with errors, result, input, events).
- **Status tiles:** running, waiting, succeeded, dead, workers. **Events panel** with filters (jobs, steps,
  workers, map, problems). Works on a phone.
- **Token:** Helios asks for `ARGUS_WORKER_TOKEN` once (if set) and keeps it in the browser; password login
  comes in C11.
- **API:** `/events` gains `component=` (sent or received by) and `newest=true`; the WebSocket accepts
  `component=` too.
- **Dev:** `helios/` source, `dev.ps1 helios` to rebuild, CI type-checks and builds it.
- **Tests:** 135.

## 0.3.0 (2026-09-28)

Core step C5: live events and the growing map.

- **Live event stream:** WebSocket `/ws/events` pushes every change (jobs, steps, workers, components) as
  it happens, typically within 0.1 s. Filters by kind (`kinds=job.,worker.`) or job. Reconnect with
  `since=<seq>` and missed events are replayed from the database, so a viewer never loses one. A viewer
  that falls too far behind is disconnected (and simply reconnects), so it can never slow Argus down.
- **The map grows by itself:** new `edges` table (migration 0002). The first time two components talk, an
  `edge.added` event appears; plugins get a box the first time a job is queued for them. `GET /map` returns
  boxes, lines, job counts per plugin and the `seq` to stream from.
- **More events:** `worker.online` / `worker.offline`, `component.added`, `edge.added`.
- **New endpoints:** `/events` (paged history), `/map`, `/registry`, public `/status`.
- **Live home page:** status cards and a live event feed. With a token set, open `/?token=<token>`.
- **`argus-events`** (`dev.ps1 events`): watch events live in a terminal, with colours, filters and
  auto-reconnect.
- **Retention:** events older than `events.retention_days` (default 90) are deleted in small chunks; the
  map's edges are kept.
- **Stability:** event IDs stay in order even if the clock steps back; Argus stops at once even with viewers
  connected (a closed viewer used to linger for up to 20 s and delay shutdown); a hung test now fails after
  120 s instead of hanging the run.
- **Dev:** `dev.ps1` reinstalls dependencies automatically when they change (this version adds
  `websockets`).
- **Tests:** 133.

## 0.2.0 (2026-09-28)

Core step C4: workers.

- **Worker protocol (HTTP):** register, long-poll claim (only jobs whose plugin the worker has), start,
  heartbeat, step reports, succeed, fail, wait; plus submit, list, counts, events, cancel, rerun and resume
  for apps and Helios. Errors are plain: 404 unknown job, 409 lease lost, 429 queue full.
- **Token auth:** set `ARGUS_WORKER_TOKEN` in `.env` and every `/jobs` and `/workers` call needs
  `Authorization: Bearer <token>`. `/`, `/health` and `/version` stay public.
- **`argus-worker`:** pulls jobs and runs plugin workflows. Steps are checkpoints (`ctx.step`), so a job
  retried after a crash skips finished steps; `ctx.idempotency_key` for side effects; `ctx.wait()` parks a
  job; `PermanentError` sends it straight to dead. Heartbeats in the background, stops a job the moment its
  lease is lost, rides out Argus restarts (retries for ~30 s). Ctrl+C exits at once when idle, or after the
  current job.
- **Built-in demo plugin** (`demo.echo`, `demo.sleep`, `demo.fail`) to try it without writing code.
- **Registry:** workers and the core show up as components (the boxes Helios will draw); silent workers are
  marked offline by the watchdog. The home page lists connected workers.
- **Fix:** a write whose caller gave up (a cancelled request) could crash the database writer thread. Such
  writes are now skipped and the writer can no longer die from one bad batch.
- **Tests:** 118. New: the full API contract, auth, end-to-end jobs through a real server, wait and resume,
  and killing a worker mid-step so another worker finishes the job without redoing the finished step.

## 0.1.1 (2026-09-28)

- Home page at `/`: status, database, watchdog, job counts and links (it returned "Not Found" before).
- Logs no longer include uvicorn's `color_message` field with terminal colour codes.

## 0.1.0 (2026-09-28)

First code: the core foundation (Argus Core Design steps C1-C3).

- **C1 skeleton:** `argusd` daemon with `--check`; `argus.yaml` + `.env` config validated at startup with clear
  error messages (exit code 2); JSON-lines logs to stdout and a rotating file; `/health` and `/version`.
  Starts in about 0.5 s.
- **C2 store:** SQLite in WAL mode with one writer thread and group commit (each write in its own savepoint,
  so one failing write never undoes others); numbered migrations; the ten core tables; refuses a newer
  database.
- **C3 jobs:** the job state machine (queued, leased, running, waiting, retry, succeeded, dead, cancelled)
  with every transition checked and logged as an event in the same transaction; capability-based claiming
  with priorities; leases and heartbeats; retries with backoff and dead-letter; step checkpoints; dedupe
  keys; per-plugin queue limits; watchdog that requeues jobs from dead workers.
- **Tests:** 104 tests, including every allowed and forbidden state transition, 1,000 concurrent writes, and
  killing the process mid-write with no half-written jobs.
