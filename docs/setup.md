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
4. Put Helios on the phone: open the https address from step 2 + `/helios/` in Chrome, sign in with the token,
   then menu > **Add to Home screen** (Install). Helios now shows up in the phone's **share menu**: share a photo,
   PDF or link to it, pick where it goes, Send.
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

When the dry-run results look right, add the plugin to `plugins.live` and restart. Anything it moves or renames
can be put back with **Undo** on the job's Changes (downloads-organizer also has **Wrong folder**).

If you used the old standalone Downloads script, remove its scheduled task first:
`C:\Users\Sas\projects\downloads-organizer\uninstall-schedule.bat`.

## Ask Argus

The box at the top of Helios (Ctrl+K) and on the phone (with a microphone button): plain words like "sort
downloads", "what's running?", "anything waiting for me?", "shut down the PC". Common asks are answered at once;
anything else goes to T1 (T2 if needed), which answers from Argus's current state. A suggested action only runs
when you tap **Do it**.

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
