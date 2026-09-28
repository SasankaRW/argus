# LLM Orchestrator Plan

Sep 28, 2026 · @Sasanka

**Version 0.8** · draft · history at the end · all versions in the Argus Docs Index

## Summary

Build one small orchestrator (working name **Argus, after the hundred-eyed giant who never sleeps**) on the spare laptop: it runs every workflow as a job, sends the work to the local Qwen models on the desktop GPU, and escalates to Claude (via the `claude -p` CLI on your subscription) only for planning, advice and review. Every step is logged, and a separate Helios dashboard plus a REST API lets Life Hub, the tracker and llmclip trigger and watch jobs.

The core idea: Claude doesn't do the bulk work, it **writes and improves the playbooks** (prompts, rules, examples) the local models follow, and reviews a sample of their output. That keeps subscription usage low and makes the 12 GB models good enough.

**On Laya:** neither project called Laya is a workflow framework. [Laya by Convai](https://adevguide.com/ai-engineering/llm-agents/what-is-laya-ai-open-source-system-one-model/) is a small (\~322–421M) non-generative classifier for routing and yes/no decisions; [aayushch/laya](https://github.com/aayushch/laya) is a notification inbox app. So Laya can't be the framework. The Convai model could later be a cheap router inside Argus (Phase 4), but it isn't needed to start. Recommended base instead: plain Python + FastAPI + SQLite (same style as your organizer), with LangGraph as an optional engine for branching flows later.

## Why the current two don't work well

Both failures are plumbing, not model quality — exactly what a shared orchestrator fixes once.

| Automation | Likely cause | Fix in Argus |
| --- | --- | --- |
| Downloads organizer | `qwen2.5-coder:14b` was never pulled, so it fell back to `qwen3:30b-a3b` (thinking model, \~54 s/file, no JSON); no one sees when it silently degrades | Model registry checks models exist at startup; every call logged with model + latency; bad-output rate shown on dashboard |
| Research agent | `pipeline_interface.py` still has 3 stub functions; config and API key never set; no scheduler | Becomes a workflow; advisor step switches from API to `claude -p`; stubs are the first thing to finish tonight |
| Both | Each script has its own model picking, retries, logging, scheduling | One shared runtime for all of it |

Also: with 12 GB VRAM only one 14B model fits at a time. Two jobs hitting different models make Ollama swap constantly. Argus needs a **single GPU queue**.

## Architecture

The laptop holds everything that must be always on; the desktop only provides the GPU, search and local file access.

&#91;embedded content: D1 v2 · early architecture · laptop, desktop, tools\]

- **The laptop is the home server.** It runs Linux, stays on for low power, and hosts Argus, Helios, Life Hub and the tracker. You reach everything from anywhere over Tailscale.
- **The PC is a GPU you switch on when you need it.** Jobs that need the GPU queue on the laptop. When enough work is waiting (or on a schedule, e.g. 8 pm), the laptop sends a Wake-on-LAN packet to the PC. The PC runs the queue, then shuts down fully (not sleep) once it has been idle for 20 minutes; see PC power cycle below.
- **While the PC is off:** urgent jobs can go straight to Claude, and tiny tasks can run on a small CPU model on the laptop (1–3B, if its CPU handles it). Everything else waits for the PC.
- **Desktop runner** is a tiny stdlib script on Windows that pulls jobs needing local files (Downloads) from Argus. It only runs while the PC is on, which is also the only time Downloads changes.
- **Claude CLI** is installed on the laptop and logged in with your subscription; Argus calls `claude -p --output-format json` with a turn cap. A daily call budget protects your plan's usage limits, and the dashboard warns if the login expires.
- **Ollama on the PC** needs `OLLAMA_HOST=0.0.0.0` and a firewall rule allowing only the laptop (LAN or Tailscale IP).
- **Power cuts:** the laptop has no working battery, so set "power on after AC loss" in its BIOS and run every app as a systemd service so everything comes back on its own. A small UPS is optional.

## Model tiers and who advises whom

Each step runs at the cheapest tier that passes its checks; a failure retries one tier up, and Claude's advice is saved so the next run doesn't need it.

| Tier | Model | Good for | Advised / reviewed by |
| --- | --- | --- | --- |
| T0 | Rules, regex, extension map | Obvious cases (e.g. `.exe` → Installers) | T3 writes the rules |
| T1 | `qwen2.5-coder:7b` (or `gemma3:4b` for Sinhala) | Classify, extract, short rewrites | T2 on retry, T3 nightly |
| T2 | `qwen2.5-coder:14b` | Summaries, code, multi-step reasoning | T3 on retry and nightly |
| T3 | Claude via `claude -p` | Plan, write playbooks, review, hard cases | You (approve in dashboard) |

The advice chain per tier is a setting in the dashboard, not hard-coded, so you can change "T1 asks T2" to "T1 asks T3" per workflow.

&#91;embedded content: D2 v1 · guidance loop · escalation and nightly review\]

Three ways Claude guides the local models:

1. **Plan once** — for a new workflow, Claude writes the playbook (system prompt, rules, few-shot examples, JSON schema).
2. **Rescue** — when checks fail twice, Claude gets the input, the bad output and the error, and returns the answer plus a rule to add.
3. **Review** — nightly, Claude grades a sample (e.g. 10 runs per workflow) and proposes playbook edits you approve with one click.

Checks are cheap code, not LLM calls: valid JSON, category in allowed list, file exists, score improved.

## Workflow format (how you add new ones)

A workflow is one YAML file in `workflows/` plus optional Python step functions; drop in a file and it appears in the dashboard. No framework lock-in.

```yaml
id: downloads-organizer
schedule: "*/30 * * * *"
runner: desktop            # needs local files
steps:
  - id: scan
    type: code             # plain Python, no LLM
    fn: organizer.scan
  - id: classify
    type: llm
    tier: T1
    escalate_to: [T2, T3]  # advice chain for this step
    playbook: playbooks/downloads.md
    output_schema: {category: enum(Documents, Images, ...)}
    check: organizer.valid_category
  - id: move
    type: code
    fn: organizer.move
    dry_run: true          # flip in dashboard
review:
  sample: 10               # Claude grades 10 runs nightly
  approve_playbook_edits: manual
```

Step types to start with: `code`, `llm`, `claude` (always T3), `search` (SearXNG), `approve` (pauses until you click in the dashboard), `notify` (Discord). Playbooks are markdown files under version control, so every Claude edit is a diff you can revert.

## Dashboard and API

Helios is its own React app (same stack as Life Hub) on top of the Argus API; Life Hub only embeds small widgets.

| Page | Shows | You can |
| --- | --- | --- |
| Live | Running jobs, current step, GPU queue, which model is loaded | Cancel, pause the queue |
| Workflows | Each workflow, schedule, success rate, avg time | Run now, enable/disable, flip dry-run |
| Tiers | Tier → model mapping and the advice chain per workflow | Change "who advises whom", swap models |
| History | Every run with steps, prompts, outputs, tier used | Re-run, undo, mark wrong (feeds the review) |
| Reviews | Claude's grades and proposed playbook edits as diffs | Approve / reject |
| Budget | Claude calls per day vs your cap, Ollama time | Set caps |

API endpoints your other tools use:

- `POST /runs {workflow, input}` — start a job (tracker: "summarise this issue")
- `GET /runs/{id}` and `GET /runs/{id}/events` (SSE) — status and live steps
- `GET /workflows`, `GET /stats` — for Life Hub widgets
- `POST /log` — llmclip reports its direct calls so history stays complete

## Helios — the overseeing dashboard

The dashboard is named **Helios**, the sun god who saw everything that happened on earth. It watches over Argus the way Helios watched over the world. It is built around a live map of every component, with details opening beside it rather than cluttering the screen.

&#91;embedded content: D5 v1 · Helios layout sketch · the mockup is the current design\]

### Views

- **Live map:** every part of the system is a node: your apps, Argus, the GPU queue, each model tier, Claude, ntfy, SearXNG and the PC's power state. Lines animate while messages flow. Thickness shows volume and colour shows status (ok, slow, failing). Click a node for its stats, or a line for its last messages.
- **Run graph:** each job drawn as its steps (scan → classify → move), lighting up live, with retries and escalations shown as branches. You can replay a run, or compare two runs side by side.
- **Conversations:** the actual back-and-forth between models for a run, shown like a chat with tier badges: T1's answer, the failed check, T2's advice, Claude's review.
- **Timeline:** a waterfall of every step and model call, so you can see where time goes.
- **Models + tiers:** the advice chain is edited *on the graph*. Draw a line from T1 to Claude, and T1 now escalates straight to Claude for that workflow.
- **Workflow builder:** drag steps to build a workflow. It writes the YAML file, and editing the YAML updates the picture.

### Customize everything

- A widget grid you can drag, resize, add to and remove from, with saved layouts ("Night shift", "Phone", "Debug").
- Saved views and filters on every page (e.g. "only failed runs using Claude this week").
- Map layout: automatic, or drag nodes and pin them; group nodes by machine, tier or workflow.
- Theme, accent colour, density and which metrics show on nodes.
- Alert rules: what goes to ntfy, at which priority, and quiet hours.
- Every setting is also a file in Git, so a bad change is one rollback away.

### How it sees everything

Every model call, message and step in Argus writes one event: run, step, parent step, **from** and **to** component, kind (prompt, reply, advice, review, check), model, tokens, time and status. Events go into SQLite and stream live to Helios over a WebSocket. "Who talked to whom" is just those from/to pairs. This is the same shape as standard tracing (spans), so it could later be exported to tools like [Arize Phoenix or Langfuse](https://openobserve.ai/blog/llm-observability-tools/) if you ever want them.

### Stack

React (same as Life Hub) + [React Flow](https://reactflow.dev/learn/layouting/layouting) for the map and run graphs, ELK.js for automatic layout, react-grid-layout for the customizable widgets, and a WebSocket from Argus for live updates.

**Keeping it clean:** dark by default, one accent colour, and animation only on live lines. Details go in the inspector, not on the canvas, and idle parts fade back.

### Connected apps

Every app you own talks to Argus the same way, so each one shows up in Helios as a node with its own traffic.

| App | Sends to Argus | Gets back |
| --- | --- | --- |
| Life Hub | widget refreshes, "run now" clicks | job status, morning brief card |
| Tracker | new issues, "summarise" requests | labels, priority, draft plan |
| Cashly | forwarded bank SMS, receipt photos | categorised entries (you approve odd ones) |
| llmclip | a log line per call | nothing (stays direct to Ollama for speed) |
| Open WebUI | chats via LiteLLM | answers; can also queue Argus jobs through MCP |

How: each app gets a tiny client (about 30 lines) that calls `POST /runs` and `POST /events`. Cashly moves to the laptop with the other apps. Helios shows Cashly in full: month spend against budget, today's spend, recent entries with amounts, and an approvals queue. Entries parsed below 0.80 confidence wait for you: approve, change the category or reject, in Helios or from the ntfy buttons on your phone. An Open Cashly button links to the Cashly web app on the laptop over Tailscale (Cashly is the source of truth; Helios reads and writes through its API).

## Supporting tools

Four small tools on the laptop take care of alerts, monitoring, model routing and chat, so Argus only has to handle workflows.

| Tool | What it does for you | How it connects |
| --- | --- | --- |
| **ntfy** | Phone push for failures, finished jobs and approvals; tap Approve / Reject / Wake PC from anywhere | Argus's `notify` and `approve` steps post to it; buttons call the Argus API over Tailscale |
| **Uptime Kuma** | One status page for every app, Ollama and the Claude login; alerts when something goes down | Pings each service; sends alerts through ntfy |
| **LiteLLM proxy** | One OpenAI-style address for all local models; swap a model in one config instead of in every app | Argus, Life Hub, llmclip and Open WebUI all call LiteLLM, which calls the PC's Ollama |
| **Open WebUI** | ChatGPT-style chat with your local models from phone or office | Runs on the laptop, talks to LiteLLM; replaces Odysseus |

Two things to know:

- **LiteLLM can't use your Claude subscription.** It needs an API key for Claude. So `claude -p` stays a separate T3 step inside Argus. If you add an API key later, Claude just becomes one more model in LiteLLM.
- **When the PC is off,** Open WebUI and LiteLLM have no local models. Open WebUI shows them offline, and a **Wake PC** button (dashboard + ntfy) sends Wake-on-LAN and the models return after a cold boot, about 1–2 minutes.

## PC power cycle

The PC is fully off by default. Argus wakes it only when there is GPU work and shuts it down when the work is done.

&#91;embedded content: D4 v1 · PC power cycle · wake, work, shut down\]

**What wakes it:** enough queued jobs (e.g. 3+), a nightly window for batch jobs (e.g. 9 pm), or the Wake PC button. Batching everything into one or two windows a day saves the most power.

**How it shuts down:** the desktop runner checks that the queue is empty, no GPU job is running and there has been no keyboard or mouse input for 20 minutes. It sends an ntfy warning with a **Cancel** button, then runs `shutdown /s /t 300`; Cancel runs `shutdown /a`. The dashboard and ntfy also get a **Shut down now** button.

**Safety rule:** auto-shutdown only happens in sessions Argus started. If you powered the PC on yourself, it stays on.

**Getting Wake-on-LAN to work from full shutdown** ([Microsoft](https://learn.microsoft.com/en-us/troubleshoot/windows-client/setup-upgrade-and-drivers/wake-on-lan-feature), [Eleven Forum guide](https://www.elevenforum.com/t/if-you-are-at-all-interested-in-wake-on-lan-wol-read-this.19662/)):

- Use wired Ethernet; Wi-Fi usually can't wake from full shutdown.
- BIOS: enable Wake-on-LAN / Power On by PCI-E, and disable ErP (it cuts power to the network card when off).
- Network adapter: enable Wake on Magic Packet and Shutdown Wake-on-LAN, and disable Energy Efficient Ethernet.
- Windows Fast Startup changes what "shut down" means. Test WoL with it off first, and turn it on only if waking fails.
- **Big gotcha:** after a wake the PC sits at the login screen, and Ollama normally starts only after login. Run Ollama and the desktop runner as startup tasks that run without login (Task Scheduler "At startup", or NSSM as services) with `OLLAMA_HOST` set system-wide.

## Automations to add

These are ranked by how much time they save you. Most use the local models in the nightly PC window, so results are ready in the morning with no extra power use.

| # | Automation | Trigger | Who does the work | You get |
| --- | --- | --- | --- | --- |
| 1 | **Morning brief** | 7 am, laptop | Built from last night's results; no GPU needed | One ntfy + Life Hub card: digest highlights, open issues, yesterday's spending, failed jobs |
| 2 | **Daily digest, moved in** | Nightly window | SearXNG + T2 summarises, T1 filters | Same Discord digest, now logged and retried on failure |
| 3 | **Issue triage** | New issue in the tracker | T1 labels + priority + duplicate check; T2 drafts first steps | Issues arrive pre-sorted, with a suggested plan |
| 4 | **Bank SMS to Cashly** | SMS forwarded from phone (SMS-forwarder app or ntfy/Tasker) | T0 regex, T1 for odd formats | Transactions logged and categorised automatically |
| 5 | **Receipt / bill capture** | Photo shared to a Argus upload link | OCR on laptop, T1 extracts amount, date, shop | Entry in Cashly, file stored |
| 6 | **Freelance / startup weekly report** | Sunday night | T2 summarises tracker + Git activity per client | Report you can edit and send to clients |
| 7 | **Website health** | Nightly | Uptime Kuma + broken-link and Lighthouse checks; T1 writes the summary | "All good" or a list of what broke on your startup sites |
| 8 | **Code review helper** | Push / PR on your repos | T2 first pass; Claude only on PRs you tag | Review notes on the PR or in the tracker |
| 9 | **Incident helper** | Uptime Kuma alert | `claude -p` with read-only access to logs | ntfy: likely cause + suggested fix |
| 10 | **Voice note to task** | Voice note from phone | faster-whisper on PC, T1 turns it into a task | Task in the tracker with the audio attached |
| 11 | **Research agent** | Nightly window | Local scout, Claude advises | TS-AUC results + changelog in the morning |
| 12 | **Downloads organizer** | While the PC is on | T0 + T1, as today | Tidy Downloads, undo in the dashboard |

Keep office data out of this: nothing from the DirectFN laptop or work repos goes through these workflows.

## Dashboard features

On top of the pages above, these make it quick to use from your phone:

- **Power panel:** PC on/off state, Wake / Shut down buttons, hours on this week and a rough kWh estimate.
- **Inbox:** one list of approvals, failed jobs and Claude's playbook suggestions, each with one-tap actions (also sent as ntfy buttons).
- **Quick run:** a command box ("summarise this link", "triage issue 42") that starts any workflow with an input.
- **Run now or next window:** choose to wake the PC now or add the job to tonight's batch.
- **Replay with another model:** re-run a past job on a different tier to compare results before changing the playbook.
- **Claude meter:** calls used today vs your cap, plus a warning if the CLI login has expired.
- **Installable phone app (PWA)** served over Tailscale, with dark mode.

Open WebUI adds a few for free: schedule prompts to run on a timer, knowledge bases from your project notes, voice input, and tools/MCP so you can ask "run the downloads organizer" from chat ([features](https://docs.openwebui.com/features/)). LiteLLM adds [automatic fallbacks, retries and timeouts](https://docs.litellm.ai/docs/proxy/reliability), e.g. 14B fails, retry on 7B.

## More automations (round 2)

These come from what other self-hosters actually run ([2026 homelab AI stack](https://dev.to/signal-weekly/the-homelab-ai-stack-in-2026-what-self-hosters-are-actually-running-2d58)), matched to your Life Hub, Cashly, freelance work and model testing.

| # | Automation | How it works | You get |
| --- | --- | --- | --- |
| 13 | **Model scout** | Watches Ollama / Hugging Face releases, estimates Q4 size against 12 GB, and runs any model that fits through your eval sets overnight | "New model X: fits, 8% better at triage, 2x slower" — no more manual model hunting |
| 14 | **Email triage** | Gmail over IMAP or API; T1 flags client / urgent / newsletter; T2 drafts replies into Drafts, never sends | ntfy only for emails that matter, with a draft ready |
| 15 | **Wishlist price watch** | Life Hub wishlist items become [changedetection.io](https://github.com/dgtlmoon/changedetection.io) watches (it handles JS sites and restock alerts) | ntfy "price dropped" with the new price |
| 16 | **CSE watchlist** | Watches Colombo Stock Exchange announcements for stocks you pick; T2 summarises filings | One-line summaries in the morning brief |
| 17 | **Read-later + summaries** | Save links from phone or browser to [Karakeep](https://docs.karakeep.app/), which tags and summarises them with your Ollama | A searchable library that feeds the digest and research agent |
| 18 | **Ask my notes** | Open WebUI knowledge base over project notes, Karakeep, digests and run history | "What did I decide about WoL?" answered from your own data |
| 19 | **Weekly review** | Sunday: Claude reads the week's tracker, Cashly, runs and inbox | A short plan: what slipped, top 3 next week, spending notes |
| 20 | **Log watcher** | T1 scans laptop and app logs nightly for errors and anomalies | Problems flagged before they break something |
| 21 | **Client invoice helper** | From tracked hours or closed issues per client, T2 drafts an invoice line list | Draft invoice to review, linked to Cashly income |

## File and money tools (chosen)

All four are built on one reusable **folder watcher** taken from the downloads organizer. It watches the folders, waits until a file has been unchanged for 2 minutes, runs the workflow, starts in dry-run, and keeps an undo log. Results that need you go to the Helios Approvals inbox and to ntfy.

| Tool | Trigger | How it works | Needs you? | Output |
| --- | --- | --- | --- | --- |
| **Screenshot renamer** | New file in Pictures\\Screenshots or on the Desktop | OCR with Tesseract, then T1 names it from the text (3 to 6 words). A small vision model is the fallback for screenshots with almost no text. | No; renames apply automatically and can be undone | `2026-09-28 cashly login bug.png`, original name kept in the log |
| **PDF and bill filer** | New PDF in Downloads or the Drop folder | Page 1 text (OCR if scanned). T0 rules for known senders (CEB, Dialog, water board), T1 for the rest: document type, vendor, date, amount, due date. The amount must appear in the page text, or the check fails. | Filing is automatic for known senders. Every bill becomes a Cashly entry that waits for approval | `Bills/2026/2026-09 CEB bill.pdf`, a Cashly entry, and an ntfy reminder before the due date |
| **Duplicate finder** | Weekly, in the night window | Code only, no model: group by size, then quick hash, then full SHA-256. Keeps the copy inside your organized folders, else the oldest. | Yes, one approval for the batch | Deleted files go to the Recycle Bin (restorable), and the list stays in history for 30 days |
| **Invoice builder** | Button in Tracker or Helios, or the 1st of each month | Pulls the client's closed issues and hours, with the rate from the client's config. T2 writes the line-item wording; totals are calculated in code, never by the model. PDF from an HTML template. | Yes, you review the draft | `Clients/<client>/Invoices/INV-2026-014.pdf`, plus expected income in Cashly once approved |

Build order: folder watcher (from the organizer) → screenshot renamer → bill filer → duplicate finder → invoice builder. The first three fit into Phase 1–2; the invoice builder needs the tracker to record hours per client.

Open question: vision fallback for screenshots. `qwen2.5vl:7b` should fit next to nothing else in 12 GB; check before relying on it.

## Argus features worth adding

- **MCP server:** expose Argus as an MCP tool so Claude (here or in Claude Code) and Open WebUI can queue jobs, read history and approve items by chat. Example: "Argus, triage my open issues tonight."
- **Eval sets per workflow:** every run you mark correct in History goes into a small test set. Model scout and any playbook change are scored against it before going live. This is what makes model swaps safe.
- **Playbook versions:** each workflow shows which playbook version produced each result, with one-click rollback.
- **Job cost:** GPU seconds, estimated energy and Claude calls per job, so you can see which workflows are worth the power.
- **Secrets in one place:** API tokens (Gmail, GitHub, ntfy) in one encrypted `.env` on the laptop, never inside workflow files.
- **Backups:** nightly copy of the SQLite databases, playbooks and configs (restic/Backrest) to an external drive or cloud.

Skip for now: n8n (Argus covers it), Home Assistant (no smart-home need stated), Immich (too heavy for the laptop's ML).

## Build phases

Start by porting what already exists, so the two broken automations become the test cases for the framework.

&#91;embedded content: D3 v2 · original build phases · superseded by D7 in the development plan\]

The advisor tier comes last on purpose: escalation is only worth building once history shows where the local models actually fail.

**Plugins:** Phase 1 builds the core only (queue, tiers, event log, approvals, power, plugin loader and `ctx` API). Every feature after that, including the downloads organizer, bill filer and Cashly connection, is added as a plugin without changing core. How to write one: Argus Plugin Guide.

## Tonight's checklist

When the desktop is on, link this session to it and connect `C:\Users\Sas\projects` so I can check the current state before writing code.

- [ ] Desktop: `ollama list` — confirm `qwen2.5-coder:14b` and `:7b` are pulled
- [ ] Desktop: set `OLLAMA_HOST=0.0.0.0`, restart Ollama, firewall rule for port 11434 from the laptop only
- [ ] Desktop: on wired Ethernet, enable WoL in BIOS, disable ErP, set the adapter options, note the MAC; shut down fully and test waking it from the laptop with \`wakeonlan \<MAC>\`; set Ollama to start at boot without login
- [ ] Laptop: confirm Linux, Tailscale, Docker, Python 3.11+ and Node are working
- [ ] Laptop: install Claude Code and log in, then test `claude -p "say hi" --output-format json`
- [ ] Phone: install the ntfy app and pick a private topic name
- [ ] Desktop: open `structural_break_real_time.ipynb` so the 3 stubs can be filled
- [ ] Decide the daily Claude call cap (suggest 30 to start)

Then Phase 1 starts: Argus core + the organizer port.

## Document history

| Version | Date | Change |
| --- | --- | --- |
| 0.8 | 2026-09-28 | File and money tools; link to the plugin guide |
| 0.7 | 2026-09-28 | Helios dashboard section (D5), connected apps, full Cashly view with approvals |
| 0.6 | 2026-09-28 | Renamed Conductor to Argus (D1 v2, D3 v2) |
| 0.5 | 2026-09-28 | More automations (round 2) and Argus features |
| 0.4 | 2026-09-28 | PC shuts down instead of sleeping (D4); automations round 1 |
| 0.3 | 2026-09-28 | Supporting tools: ntfy, Uptime Kuma, LiteLLM, Open WebUI |
| 0.2 | 2026-09-28 | Laptop becomes the always-on server; Tailscale |
| 0.1 | 2026-09-28 | Created |
