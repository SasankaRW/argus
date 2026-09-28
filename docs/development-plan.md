# Argus Development Plan

Sep 28, 2026 · @Sasanka

**Version 1.2** · the working tracker · history at the end · all versions in the Argus Docs Index

## Summary

Lean first, then grow. The essentials land in about 7 weeks of evenings and weekends (assumed about 10 hours a week), and the Helios live map is there from week 3 and grows with every part we add. Everything else is added later, when you actually need it.

- **M0 to M3** run on the PC: core with the live map, then the first plugins, then the money plugins with Cashly.
- **MD** moves everything to the laptop and switches on the real power cycle, backups and the essential security.
- **Grow** is open-ended: more plugins, more Helios views and the heavier infrastructure, each added when its absence is felt.

Each milestone ends with a gate: a concrete test that must pass before the next one starts. Design references: LLM Orchestrator Plan and Argus Plugin Guide.

## Roadmap

Weeks are relative to the start date. If a week slips, everything after it moves; no milestone is skipped to catch up.

&#91;embedded content: D7 v4 · roadmap · lean path, 7 weeks to the laptop, then grow\]

MD can move earlier if the NVMe arrives sooner, but not before M1 is done. Grow has no end date: pick the next item from its list whenever something is missing.

## Architecture

After the move to the laptop, every app and device talks only to Argus core on the laptop. The PC is woken when there is GPU or file work, and Claude is reached only through the locked-down `claude -p` on the laptop.

&#91;embedded content: D8 v2 · system architecture after MD · phone, laptop, PC, cloud\]

- **Phone:** Helios, approvals and alerts reach the laptop over Tailscale. The SMS forwarder sends bank messages to a signed webhook on the API.
- **Argus core** holds the API, queues, event log, plugin loader, approvals, model router, power manager, backups, and secrets and access control. Plugins run in their own processes and reach everything through `ctx`.
- **Model calls:** the router sends T1 and T2 work through LiteLLM to Ollama on the PC over the LAN, one model at a time. T3 work goes through `claude -p` to Anthropic.
- **PC:** the desktop runner (as `llmsvc`) handles file jobs in your folders and receives the nightly backups. SearXNG also runs here for research and digest plugins (not drawn).
- **During M0 to M3** the same parts all run on the PC; only the addresses in the config change at deploy.

The diagram shows the full target. LiteLLM, Open WebUI and Uptime Kuma arrive in the Grow phase; until then the model router talks to Ollama directly.

## Repo layout and tech stack

One private Git repo holds everything, so a single commit can change core, a plugin and Helios together.

```
argus/                     # repo root
  core/                    # Python service on the laptop
    api/                   # FastAPI routes: runs, events, approvals, plugins, power
    engine/                # scheduler, job queue, GPU queue, workflow runner
    models/                # tier router, LiteLLM client, claude -p wrapper, checks
    plugins/               # loader, manifest schema, ctx API, permissions
    services/              # ntfy, files sandbox, secrets, store, power (WoL)
    db/                    # SQLite schema + migrations
  runner/                  # Windows desktop runner (stdlib Python, runs as llmsvc)
  plugins/                 # one folder per plugin (see the plugin guide)
  helios/                  # React + Vite front end
  cli/                     # argus command: plugin new/test/enable/live/rollback
  deploy/                  # docker-compose, systemd units, Windows task XML
  tests/                   # core tests with a mock Ollama and mock claude
  docs/                    # links to the plan and plugin guide
```

| Part | Choice | Why |
| --- | --- | --- |
| Core API | Python 3.12, FastAPI, Pydantic | same language as your existing scripts; Pydantic validates manifests and model output |
| Storage | SQLite (WAL) | one file, easy backup; enough for one user |
| Scheduling | APScheduler + own queue tables | cron plus night window, survives restarts |
| Models | LiteLLM → Ollama on the PC; `claude -p` for T3 | one address for all local models (Grow phase; M1 uses a small Ollama client) |
| Live updates | WebSocket from core to Helios | powers the live map |
| Front end | React, Vite, React Flow, ELK.js, react-grid-layout | same stack as Life Hub |
| Desktop runner | Python stdlib, Task Scheduler at startup | no install hassle; works like the organizer |
| Deploy | Docker Compose on the laptop (core, Helios, LiteLLM, ntfy, Uptime Kuma, Open WebUI) | one command up; systemd starts it at boot |
| Access | Tailscale only, plus a Helios password | nothing exposed to the internet |

**Develop on the PC, run on the laptop.** Code must work on both, so it uses `pathlib` for paths and reads every host, port and folder from config. Only the config file changes at deploy.

| Setting | PC during development | Laptop after MD |
| --- | --- | --- |
| Argus core, Helios | on the PC (native Python, or Docker if WSL2 works) | Docker Compose on Ubuntu |
| Ollama address | `127.0.0.1:11434` | PC's LAN address |
| Desktop runner | same machine | on the PC, pulling jobs from the laptop |
| Power manager | simulated (logs only) | real wake and shutdown |
| ntfy | public ntfy.sh, private topic | self-hosted on the laptop |
| Backups | none needed yet | laptop snapshot, copied to the PC, last 5 kept |

## Helios UI

The [Helios mockup](https://claude.ai/artifact/EAhWvwckWzyc6ouyPdfW6C) is the design to build in M3. It runs on simulated data; M3 connects the same screens to real events from Argus.

&#91;image: Helios overview: live map, Cashly inspector, runs, timeline and events\]

### Layout

| Area | Shows | Built from |
| --- | --- | --- |
| Top bar | search and command box (Ctrl K), PC state, GPU and VRAM, Claude calls used of 30, clock | power manager, GPU queue, Claude budget |
| Side menu | Live map, Runs, Conversations, Workflows, Apps, Models, Approvals (with count), Power, Customize; Argus health and next night window at the bottom | plugin list, approvals count |
| Summary tiles | jobs today, success rate, escalations, GPU time and energy, each with a 24-hour trend line | event log aggregates |
| Live map | every app, service and model as a box, grouped by machine or tier; lines light up and a dot travels along them while messages flow; red for a failed check, amber for advice and escalation | events with from and to |
| Inspector | the selected box or connection: Details, Messages (the model-to-model conversation), Settings | node stats, events, plugin config |
| Active runs | running and queued jobs with tier and progress | runs table |
| Run timeline | steps of the selected run as a waterfall | steps table |
| Events | live event stream | WebSocket |

### App panels and approvals

Apps can add their own panel in the inspector. Cashly's shows month spend against budget, today's spend, recent entries and its approvals, plus an Open Cashly button. The Approvals inbox collects every waiting item in the three card types from the plugin guide: entry (Cashly entries), batch (duplicates) and draft (invoices).

&#91;image: Approvals inbox with a duplicates batch, an invoice draft and a Cashly entry\]

### Design tokens

| Token | Value | Use |
| --- | --- | --- |
| Background | `#0B0D12` | page |
| Panel / raised | `#0F1218` / `#141821` | cards, boxes, bubbles |
| Lines | `#1A1F2A`, `#252B38` | borders, idle map lines |
| Text | `#E6E8EE`, `#98A0B3`, `#838B9E` | primary, secondary, muted |
| Accent (amber) | `#F5A524` | Helios brand, selection, advice and escalation, approve buttons |
| Flow (blue) | `#58B7FF` | requests, T1 |
| T2 (violet) | `#A78BFA` | T2 and model boxes |
| Good / bad | `#3DD68C` / `#FF7A6B` | success, failed checks |
| Fonts | Geist, Geist Mono | UI text; numbers, times, IDs, code |

**Rules that keep it clean:** dark only, one accent colour, and animation only on live lines. Details open in the inspector, never on the map. Status is shown by shape and colour (dot, badge, thin coloured edge) instead of big coloured boxes. At phone width the side menu becomes a scrolling row and the panels stack.

### How the map grows

The map is never drawn by hand. It builds itself from two sources, so a new part appears on it the moment it exists.

- **Boxes** come from the component registry. Core parts register at startup, each plugin registers from its `plugin.yaml`, models come from Ollama's model list and the tier config, and each app appears when its API token is created.
- **Lines** come from the event log. A line appears the first time two parts exchange a message, and grows thicker with traffic.
- **New parts** get a small "new" badge for a week, and ELK re-lays out the map automatically. Positions you pin by dragging are kept.

## Milestones in detail

Each milestone lists its tasks in build order and one gate. The next milestone starts only when the gate passes, and every gate ends with a screenshot of the live map for the Map history below.

### M0 · Dev setup on the PC (week 1)

- [ ] Repo with the layout above, basic CI (lint + tests)
- [ ] Python 3.12 and Node on Windows; Docker/WSL2 if it works, otherwise native Python
- [ ] Pull `qwen2.5-coder:7b` and `:14b`; confirm Ollama answers on `127.0.0.1:11434`
- [ ] Claude Code on the PC, logged in; test `claude -p`
- [ ] ntfy app on the phone, public ntfy.sh server with a private topic for now
- [ ] Tailscale on the PC so Helios opens on your phone while developing
- [ ] `docs/` folder in the repo, `argus docs sync` (exports docs as Markdown and diagrams as SVG, updates the changelog, tags the version) and `argus release patch|minor|major`
- [ ] Early Wake-on-LAN check from full shutdown, using a WoL app on your phone

**Progress (2026-09-28):** the repo is at `G:\Projects\argus` and goes to a private GitHub repo with `.\scripts\dev.ps1 github`. Version control is in place: work on branches, pull requests squash-merged after CI passes, `main` protected by a local pre-push hook (GitHub Free doesn't enforce branch rules on private repos), one-command releases (`dev.ps1 release patch|minor|major`: tests, version bump, changelog, tag, GitHub release), and the design docs exported to `docs/`. Argus is at `0.4.0` with core steps C1–C6 done ahead of M1. Still open in M0: Docker/WSL2 check, Claude Code test, ntfy, Tailscale and the WoL test.

**Gate:** a script gets answers from Ollama and `claude -p` and sends an ntfy message; WoL from full shutdown works.

### M1 · Core + live map (weeks 2–4)

Built in the 12 steps of the Argus Core Design (durable SQLite queue, leases, checkpoints, outbox, fault-injection and soak tests). It is a third week longer than first planned, because stability is the point.

- [ ] SQLite schema: runs, steps, events, approvals, components
- [ ] Job queue, scheduler, night window, single GPU queue
- [ ] Model router: small Ollama client, tiers T1/T2, output schemas and checks, escalation; `claude -p` locked down with a daily cap
- [ ] Event log with from/to on every message, streamed over WebSocket
- [ ] Component registry: core parts, models, plugins and apps register themselves so the map knows what exists
- [ ] Approvals + ntfy Approve/Reject buttons (one-time tokens)
- [ ] Plugins as plain folders with a small `plugin.yaml` and a folder allowlist (no process isolation yet)
- [ ] Power manager in simulated mode
- [ ] **Helios v0:** live map (React Flow + ELK auto-layout, in the mockup style), approvals inbox, event list, password login

**Gate:** a test workflow runs end to end. T1 fails a check, escalates to T2, asks for approval on your phone, you approve, and it writes a file. The whole run plays out on the live map.

### M2 · First plugins (week 5)

- [ ] Port the downloads organizer as the first plugin (dry-run first)
- [ ] Screenshot renamer plugin
- [ ] Helios: runs list and inspector (details and messages)
- [ ] **Share to Argus:** Helios installs on the phone as an app and appears in Android's Share menu; shared photos, PDFs, links and screenshots go to the right plugin
- [ ] **"Wrong" button** on every result: you correct it, and the correction is saved as an example the plugin's instructions use next time

**Gate:** the organizer runs for 3 days with no silent fallbacks; both plugins show on the map with their traffic.

### M3 · Money plugins (week 6)

- [ ] Small API on Cashly, and the Cashly connector plugin
- [ ] Bill filer plugin
- [ ] Bank SMS plugin (phone SMS forwarder to a signed webhook)
- [ ] Cashly panel in Helios: month spend, today, recent entries, approvals, Open Cashly
- [ ] **Ask Argus:** plain-language questions and commands from the search box or phone ("how much did I spend on food this month?"); T2 routes each one to the right plugin

**Gate:** a CEB bill PDF dropped in Downloads becomes an approved Cashly entry from your phone, and you watch it travel across the map.

### MD · Deploy to the laptop (week 7, or when the NVMe arrives after M1)

- [ ] Fit the NVMe; Ubuntu Server 24.04, static IP, Tailscale, Docker; BIOS "power on after AC loss"; Docker log size limit
- [ ] Docker Compose for core, Helios and ntfy (self-hosted); systemd starts it at boot
- [ ] Move the database and settings from the PC (and Cashly)
- [ ] PC becomes the GPU worker: `llmsvc`, Ollama and desktop runner at boot without login, `OLLAMA_HOST`, firewall allows only the laptop
- [ ] Power manager goes real: wake, 20-minute idle, 5-minute warning, shutdown
- [ ] Backups: nightly snapshot copied to the PC in the night window, last 5 kept in each place
- [ ] `argus deploy` and `argus rollback`: one command backs up the database, pulls the new version, runs migrations, restarts after jobs reach a checkpoint, checks health, and rolls back automatically if the check fails
- [ ] Essential security (see Security): Tailscale ACLs, firewall, automatic updates, per-app tokens, `.env` readable only by Argus

**Gate:** the laptop wakes the PC, runs a real plugin job, and the PC shuts down; Helios works from your phone through the laptop; a backup reaches the PC; a LAN port scan finds nothing open.

### Grow (week 8 onward, pick as needed)

No fixed order. Each item is small enough for a week or less, and each one appears on the map when it lands.

- [ ] Duplicate finder
- [ ] Invoice builder (after the Tracker records hours per client)
- [ ] Tracker connector and Life Hub widgets
- [ ] Research agent and daily digest ported as plugins
- [ ] **Argus as an MCP server**, so Claude (Cowork, Claude Code) can read runs, Cashly totals and the tracker, and queue jobs
- [ ] **Time saved:** each plugin estimates minutes saved per run; Helios shows a weekly total, and unused plugins get switched off
- [ ] **Quiet by default:** only approvals and failures notify you; everything else goes into one evening summary
- [ ] Helios: run timeline, conversations view, power panel, Claude meter, customizable layout
- [ ] LiteLLM, Open WebUI, Uptime Kuma
- [ ] Plugin CLI (new, test, live, rollback) once there are 5+ plugins
- [ ] Stronger security: passkey or authenticator login, encrypted secrets file, plugin process isolation, disk encryption
- [ ] Guidance loop: eval sets, nightly Claude review, tier editor on the map, workflow builder

### Map history

A screenshot of the live map at each gate, so you can see the system grow. Added as each milestone passes.

| Milestone | What the map shows |
| --- | --- |
| M1 | core, T1, T2, Claude, ntfy, one test plugin |
| M2 | + downloads organizer, screenshot renamer, desktop runner |
| M3 | + Cashly, bill filer, bank SMS, phone webhook |
| MD | split into laptop and PC groups, power cycle on the PC node |

## Security

Security is kept light: Argus serves one person and is reachable only through Tailscale, which already keeps strangers out. These few measures are cheap and cover the real risks, mainly your money data and a model being tricked by text inside a file or SMS.

| Keep | Why | When |
| --- | --- | --- |
| Tailscale only, no port forwarding | nothing is reachable from the internet | M0, MD |
| Helios password login | someone on your Wi-Fi can't open it | M1 |
| A token per app, and a signed webhook for the SMS forwarder | only your apps and phone can send in jobs or bank messages | M1, M3 |
| Money always needs your approval; totals calculated in code | a wrong model answer can't change Cashly | M1 |
| Models return checked, structured answers and can't take actions; `claude -p` runs with tools disabled | text inside a bill or SMS can't make anything happen | M1 |
| Plugins limited to their listed folders; DirectFN and work folders blocked | a bug can't touch the wrong files | M1 |
| `.env` out of Git and readable only by Argus | tokens don't leak | M0 |
| Firewall on the laptop and PC, automatic security updates | basic hygiene | MD |
| Encrypted backups (restic does this by default) | a lost backup drive reveals nothing | MD |

**Skipped for now** (in the Grow list if you ever want them): passkeys or authenticator login, an encrypted secrets file, running plugins in separate sandboxed processes, disk encryption, and a Security page in Helios.

## How we work

Work happens in short sessions, each finishing one or two checklist items, so progress never depends on a long block of free time.

- **Each session:** pick the next unchecked task, build it, run the tests, tick it off here. I write and test code in my workspace with mock models, then move it to your PC or laptop when this session is linked to them.
- **Testing:** core and plugins are tested against mock Ollama and mock `claude`, so tests run anywhere without the GPU. Real-model checks happen at each gate.
- **Commits:** one commit per task, small and descriptive. `main` is always runnable; bigger changes go on a branch.
- **Definition of done for a task:** tests pass, the feature shows up in Helios (once M3 exists), and the plugin guide or this plan is updated if anything changed.
- **Tracking:** this document is the tracker. Your Tracker app can take over once its connector exists in M4.

## Risks and fallbacks

| Risk | Effect | Fallback |
| --- | --- | --- |
| Wake-on-LAN doesn't work from full shutdown on your motherboard | the PC can't be started remotely | hibernate instead of shutdown (almost the same power use), or a smart plug with "power on after AC loss" |
| Laptop runs out of RAM with all containers | slow or crashing services | drop Open WebUI or Uptime Kuma first; check the RAM figure in M0 |
| `claude -p` login expires or hits plan limits | T3 steps fail | Helios warns; runs fall back to T2 plus your approval; an API key stays an option |
| You're using the PC (gaming, work) when a job arrives | GPU contention, slow PC | the runner waits while you're active and the job moves to the next window |
| Qwen 7B is too weak for a task | many escalations | switch that plugin's playbook or tier; model scout later finds better fits |
| Power cut mid-run | half-finished jobs | jobs are idempotent and resume; file moves go through the undo log |

## Open decisions

Needed before the milestone shown; none of them block M0.

- [x] **Repo host** (before M0 ends): GitHub private repo, or Forgejo on the laptop later?

  Decided 2026-09-28: GitHub, private repo.
- [ ] **Laptop specs** (M0): RAM and disk size, and which Linux distro (Ubuntu Server 24.04 is the default suggestion)
- [ ] **Cashly's stack** (before M4): is it a web app with an API, or a desktop app? This decides how the connector and "Open Cashly" work.
- [ ] **Tracker hours** (before the invoice builder in M5): add hours per issue and a rate per client to the Tracker?
- [ ] **Night window time** (M1): 21:30 by default, or later?

## Document history

| Version | Date | Change |
| --- | --- | --- |
| 1.2 | 2026-09-28 | Repo host decided (GitHub, private); version control workflow, one-command releases and docs in Git done; M0 progress updated to Argus 0.4.0 |
| 1.1 | 2026-09-28 | Repo created at G:\\Projects\\argus; Argus 0.1.0 with core steps C1–C3 |
| 1.0 | 2026-09-28 | Versioning added; `argus docs sync`, `argus release` (M0) and `argus deploy` / `argus rollback` (MD) tasks; diagram IDs D7, D8 |
| 0.9 | 2026-09-28 | M1 extended to 3 weeks and linked to the core design; D7 v4 |
| 0.8 | 2026-09-28 | Share to Argus, wrong button, Ask Argus, MCP server, time saved, quiet by default |
| 0.7 | 2026-09-28 | Lean first, then grow; live map from M1; security dialed down; D7 v3 |
| 0.6 | 2026-09-28 | System architecture diagram (D8) |
| 0.5 | 2026-09-28 | Helios UI section with mockup screenshots and design tokens |
| 0.4 | 2026-09-28 | Security section |
| 0.3 | 2026-09-28 | Build on the PC, deploy to the laptop later (MD); D7 v2 |
| 0.2 | 2026-09-28 | Backups to the PC, last 5 kept |
| 0.1 | 2026-09-28 | Created (D7 v1) |
