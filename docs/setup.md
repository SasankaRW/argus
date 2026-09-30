# Setting up Argus on the PC

Everything in order, once. Commands are for PowerShell. Steps marked *optional* can be skipped and done later.

## 1. Install the tools

```powershell
winget install Git.Git GitHub.cli Python.Python.3.12 Ollama.Ollama
winget install OpenJS.NodeJS.LTS            # needed for the Claude CLI (and to rebuild Helios)
winget install tailscale.tailscale          # phone approvals and reaching Argus from anywhere
winget install UB-Mannheim.TesseractOCR     # optional: lets screenshot-renamer read text (faster than vision)
```

Close and reopen PowerShell afterwards so the new commands are found.

## 2. Get the code

```powershell
cd G:\Projects
gh auth login                               # once, so dev.ps1 can open PRs and releases
gh repo clone SasankaRW/argus
cd argus
.\scripts\dev.ps1 setup                     # .venv, Argus + tools + plugin extras, argus.yaml from the example
```

## 3. Pull the models

```powershell
ollama pull qwen2.5-coder:7b                # T1: first try for text jobs
ollama pull qwen2.5-coder:14b               # T2: when T1's answer fails the checks
ollama pull qwen2.5vl:7b                    # V1: vision (screenshot-renamer)
ollama pull nomic-embed-text                # search your files by meaning (Ari, "My files")
```

They fit the 12 GB GPU one at a time; Ollama swaps them as needed.

## 4. Claude (T3)

```powershell
npm install -g @anthropic-ai/claude-code
claude                                      # log in once with your subscription, then exit
```

Argus runs it with every tool switched off and a daily cap (`claude.calls_per_day` in argus.yaml).

## 5. Secrets: `.env`

Copy `.env.example` to `.env` (never committed) and fill in:

| Name | What |
| --- | --- |
| `ARGUS_WORKER_TOKEN` | a long random token; Helios asks for it once. Make one: `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `NTFY_TOPIC` | a long random topic name, e.g. `argus-7f3k2q9x`; subscribe to it in the ntfy phone app |
| `ARGUS_ADMIN_PASSWORD` | change from `change-me` |

## 6. Settings: `argus.yaml`

The example has comments on every line. The parts to check:

```yaml
models:
  tiers:
    T1: {provider: ollama, model: "qwen2.5-coder:7b", group: pc}
    T2: {provider: ollama, model: "qwen2.5-coder:14b", group: pc}
    T3: {provider: claude, group: cloud}
    V1: {provider: ollama, model: "qwen2.5vl:7b", group: pc}   # vision: not in the chain, asked for directly
  chain: [T1, T2, T3]

approvals:
  public_url: https://<pc-name>.<tailnet>.ts.net   # from step 7
  phone: <phone's Tailscale name>                  # e.g. pixel-8a

plugins:
  live: [downloads-organizer]   # plugins allowed to change files; the others run in dry-run
```

## 7. Phone (Tailscale + ntfy) *optional*

1. Sign in to Tailscale on the PC and the phone (same account).
2. Let the phone reach Helios over Tailscale: `tailscale serve --bg 8600`. It prints the https address; put it in
   `approvals.public_url`.
3. Install the ntfy app on the phone and subscribe to your `NTFY_TOPIC`.
4. Put Argus on the phone as an app: open the https address from step 2 + `/helios/` in Chrome, sign in with the
   token, then **More > install-app** (or Chrome's menu > **Install app**). You get:
   - an **Argus** icon that opens full screen, even when the PC is off (it then says it can't reach Argus);
   - **long-press shortcuts** on the icon: Talk to Ari (opens the current chat with the mic on), Inbox, Share, Map;
   - Argus in the phone's **share menu**: share a photo, PDF or link to it, pick where it goes, Send.
5. Test: `.\scripts\dev.ps1 ntfy` (a notification) and `.\scripts\dev.ps1 approval` (Approve / Reject on the phone).

## 8. Start it

```powershell
.\scripts\dev.ps1 up        # Ollama, argusd and the PC worker; opens Helios at http://127.0.0.1:8600
.\scripts\dev.ps1 down      # stop ("down all" also stops Ollama)
```

Or double-click `scripts\up.cmd`. Everything runs in the background (no extra windows); one summary is printed.
After changing argus.yaml or .env, run `down` then `up`.

| Command | What |
| --- | --- |
| `.\scripts\dev.ps1 status` | what is running |
| `.\scripts\dev.ps1 logs` | every log in this terminal, coloured (`logs worker` for one) |
| Helios > **Logs** | the same, with filters and search |

Logs live in `logs\` (argus.log, worker.log, ollama.log; a `-crash.log` appears only if a process died on start).

## 9. Check it works

```powershell
.\scripts\dev.ps1 models    # every tier answers (V1 too), Claude found
.\scripts\dev.ps1 demo      # a small job: shows on the Helios map
.\scripts\dev.ps1 classify  # a model job with an escalation T1 -> T2
```

## 10. Turn on the plugins

Each plugin starts in dry-run: it shows what it would do (Runs page in Helios) and changes nothing.

| Plugin | Needs | Try it |
| --- | --- | --- |
| downloads-organizer | T1, T2 | "Sort Downloads now" on its box |
| screenshot-renamer | T1, V1 (Tesseract optional) | take a screenshot, or "Name screenshots now" |
| duplicate-finder | nothing (no model) | "Find duplicates": one approval, then the extra copies go to the Recycle Bin |

When the dry-run results look right, add the plugin to `plugins.live` and restart. Anything it moves or renames
can be put back with **Undo** on the job's Changes (downloads-organizer also has **Wrong folder**).

If you used the old standalone Downloads script, remove its scheduled task first:
`C:\Users\Sas\projects\downloads-organizer\uninstall-schedule.bat`.

## Moving to the laptop

See `deploy/README.md`: install script, services, moving the database, the PC as the GPU worker, backups,
and real automatic power.

## Ask Argus

The box at the top of Helios (Ctrl+K) and on the phone (with a microphone button): plain words like "sort
downloads", "what's running?", "anything waiting for me?", "shut down the PC". Common asks are answered at once;
anything else goes to T1 (T2 if needed), which answers from Argus's current state. A suggested action only runs
when you tap **Do it**.

## Ari

Helios > **Ari** (and **Talk to Ari** on the phone): talk to Argus by typing or with the microphone. Ari answers
(aloud if **Speak replies** is on), remembers the conversation, and asks before doing anything: say or tap **Yes**.

- "What's running?", "anything waiting for me?", "sort my downloads", "shut down the PC".
- A time makes a schedule, after your yes: "sort downloads every morning at 7", "every weekday at 9 name
  screenshots", "remind me to call mum tomorrow at 5 pm" (a phone notification), "in 20 minutes remind me to check
  the oven", "shut down the PC at 11 pm". They are listed under **Your schedules** (pause or delete there).
- **"Hey Ari"**: tick it and, while Helios is open (on the PC, for example), say "Hey Ari, …". After Ari asks
  "Shall I?", just say yes or no. The browser listens for the wake phrase (Chrome or Edge; allow the microphone).

*Optional:* a natural voice and private hearing.

```powershell
pip install -e .[voice,hearing]                   # Piper (voice) and faster-whisper (hearing)
python -m piper.download_voices en_US-lessac-medium --data-dir data\voices
```

```yaml
ari:
  voice: data/voices/en_US-lessac-medium.onnx     # Ari speaks with Piper instead of the browser's voice
  hearing: whisper                                # the mic button records; Whisper on the PC writes it down
  whisper_model: small.en
```

**"Hey Ari" on the PC without a browser** (your microphone, nothing leaves the PC):

```powershell
pip install -e .[listen,voice]
```

```yaml
ari:
  listen: true            # dev.ps1 up starts it (logs\ari.log); or run: python -m argus.ari_listen
  voice: data/voices/en_US-lessac-medium.onnx   # so Ari answers out loud (else the answer is only in Helios)
```

Say "Hey Ari, what's running?" or "Hey Ari" (a chime), then what you want. When Ari asks "Shall I?", just say yes
or no. A small Whisper model (`ari.listen_wake_model`, tiny.en) listens for the wake phrase on the GPU; the command
uses `ari.whisper_model`. Pick another microphone with `python -m argus.ari_listen --device <n>` (`python -m
sounddevice` lists them).

Whisper uses the GPU when CUDA 12 and cuDNN 9 are found, else the CPU (fine for short commands). While the PC is
off, the browser hears you. Other voices: https://rhasspy.github.io/piper-samples/

**Ari's popup over the whole screen** (like Siri: a glowing pill at the top of the screen while Ari listens,
thinks, works or talks, over any app; tap it to open Ari in Helios):

```powershell
pip install -e .[popup]
```

```yaml
ari:
  popup: true             # dev.ps1 up starts it; or run: python -m argus.ari_popup
```

It follows Ari everywhere: "Hey Ari" on the PC, a question typed in Helios on the phone, the tool Ari is using, the
answer. It never takes the keyboard. While it runs, Helios in a browser on the PC leaves the pill to it. Helios on
other screens (the phone) shows the same pill at the top of the page.

## What Argus tells you by itself

- **Morning brief** (`brief.at`, 07:00): overnight results, failures, what waits for you, today's schedules, the
  backup, the PC and Docker, yesterday's Claude calls. `POST /brief` sends one now to try it.
- **Evening summary** (`summary.at`, 20:00, a quiet notification): today's jobs, about how much time they saved
  you, what failed or waits; on Sundays the week's total too. `POST /summary` sends one now.
- **Time saved**: each plugin estimates the minutes a job saved you (moving a file ~20 s, naming a screenshot
  ~30 s, ...). Helios shows this week's total ("Saved you") and the phone view a card.
- **After a power cut or crash**: "Argus is back": when it stopped, which jobs pick up from their last finished
  step, which missed schedules run now. (Not after `dev.ps1 down` or an update: only when it didn't stop cleanly.)
- **PC health** (`health.enabled: true`, `health.containers: [eclaire-app, eclaire-db]`): the PC's worker looks at
  WSL and Docker every 30 minutes while the PC is on (it never wakes the PC for it) and tells the phone when a
  container stops, reports unhealthy, or Docker stops answering, and again when it's fine.

## Ari: questions and things on the PC

Ari answers anything now, and can use tools: Argus's own data (what ran, schedules, time saved, where your phone
is) and whatever plugins offer (`ari: tools:` in their plugin.yaml). Your own things come first; general knowledge
it answers itself; current things (news, weather, scores) go to Claude with web search (read only), within the
daily cap.

- **My files** (`knowledge`): "what did I write about the laptop server?", "find my invoice from October". It
  indexes Documents, Desktop, Notes and G:\Projects (contents: text, Markdown, code, Word, PDF) every night at 2:15
  (or "Index my files now"), and Downloads, Pictures, Videos, Music by file name only; the index stays on the PC.
  For search by meaning too: `ollama pull nomic-embed-text` (without it, search is by words). Folders: Helios >
  My files settings (`folders`, `names_only`). It only reads, so it works in dry-run.
- **PC apps** (`pc-apps`): "open Spotify", "open my CV in Documents", "open youtube.com", "close Chrome" (asks first).
- **PC media** (`pc-media`): "volume 30", "turn it down", "mute", "next song", "pause".
- **PC windows** (`pc-windows`): "switch to VS Code", "show the desktop", "lock the PC", "take a screenshot".
- **PC keyboard** (`pc-keys`): "type 'see you at 6' ", "press ctrl+s" in the window in front (always asks
  first; never types passwords).
- **PC status** (`pc-system`): "how's the PC doing?", "what's using my GPU?", "how much space is left on G?",
  "what's on my clipboard?", "copy that to the clipboard" (asks first).

They run in your logged-in Windows session (the worker `dev.ps1 up` starts has it; after the move to the laptop,
`pc-worker.ps1 install` adds an "Argus desktop" task at logon). Like every plugin they start in dry-run: add them
to `plugins.live` (`live: [downloads-organizer, pc-apps, pc-media, pc-windows, pc-system, pc-keys]`). Anything that closes, types or changes files
asks you first.

## Argus in Claude (MCP)

`.\scripts\dev.ps1 mcp` adds Argus to Claude Code as an MCP server (`http://127.0.0.1:8600/mcp`, with your
token). Then ask Claude Code things like "what did Argus do today?", "why did the last screenshot job fail?" or
"sort my downloads". Tools: `argus_status`, `list_jobs`, `get_job`, `read_log`, `list_approvals`,
`list_schedules`, `time_saved`, `list_buttons`, `run_button`, `ask_argus`. Claude can't approve anything, power
the PC or touch files: approvals stay yours.

## Find my phone

Ask Ari "where's my phone?" (or Helios > Power > Your phone): where Tailscale sees it (online at home on the same
Wi-Fi, online away, or offline and when it was last online), then **Ring my phone** sends three urgent
notifications 20 s apart. To hear them on silent: ntfy app > your topic > Notification settings > allow
"Override Do Not Disturb" for urgent messages. Needs `approvals.phone` (the phone's Tailscale name).

## PC power buttons

Helios > **Power** (and the **PC power** card on the phone): **Sleep**, **Restart**, **Shut down** (waits
`power.shutdown_delay_seconds`, 60 s, with a **Cancel** button) and **Wake**. They run on the PC's worker, ahead of
any other job (a job already running finishes first). **Wake** sends Wake-on-LAN from the machine running argusd:
set `power.pc_mac` (from `ipconfig /all`, the wired card) and turn on Wake-on-LAN in the BIOS and the network
card. It is useful once argusd runs on the laptop; while argusd runs on the PC, shutting the PC down stops Argus too.

## Problems

| Symptom | Fix |
| --- | --- |
| Helios shows the login again | the token in `.env` changed: enter the new one |
| `models` says a tier is not pulled | `ollama pull <model>` from step 3 |
| `models` says Claude not found | step 4, then reopen PowerShell |
| Phone buttons don't work | the phone must be on Tailscale; check `approvals.public_url` |
| A plugin changes nothing | it is in dry-run: add it to `plugins.live`, then `down` / `up` |
| screenshot-renamer always uses V1 | Tesseract not installed (fine, just slower) |
