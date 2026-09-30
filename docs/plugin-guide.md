# Argus Plugin Guide

Sep 28, 2026 · @Sasanka

**Version 0.5** · draft (plugin API 1.0 draft) · history at the end · all versions in the Argus Docs Index

The reference for adding features to Argus. Keep it updated whenever the plugin API changes or a plugin is added.

## Core vs plugins

Argus core stays small and stable; every feature is a plugin in its own folder that core discovers, checks and runs. A plugin never talks to a model, a file or another app directly. It goes through the `ctx` API, so every action is logged, limited and visible in Helios.

| Lives in core (build once) | Is a plugin (add any time) |
| --- | --- |
| Job queue, scheduler, single GPU queue | Downloads organizer, screenshot renamer, bill filer |
| Model tiers T0–T3, escalation, `claude -p` wrapper | Duplicate finder, invoice builder, research agent |
| Event log (SQLite) + WebSocket stream | Connectors to your apps: Cashly, Tracker, Life Hub |
| Approvals inbox, ntfy, permissions, secrets | Daily digest, email triage, model scout |
| PC power (wake, shutdown), desktop runner | Anything new you think of later |
| Plugin loader, `ctx` API, Helios |  |

**Build order:** core first (Phase 1), with the downloads organizer as the first plugin to prove the API. After that, each feature is added as a plugin without touching core. If a plugin needs something core can't do, add it to core as a new `ctx` service, bump the API version and note it in the changelog at the end of this guide.

There are three kinds of plugin:

- **Workflow**: does a job (bill filer, duplicate finder).
- **Connector**: wraps one of your apps and offers actions to other plugins (the Cashly connector provides `cashly.add_entry`).
- **Trigger source**: brings events in (bank SMS webhook, folder watcher variants).

## How a plugin connects

The loader reads each plugin's manifest, refuses anything that asks for more than it declared, and hands the plugin a `ctx` object limited to those permissions.

&#91;embedded content: D6 v1 · how a plugin connects · loader, ctx API, core services\]

Because everything passes through `ctx`, a new plugin automatically gets logging, retries, tier escalation, approvals and a node on the Helios map, with no extra code.

## Plugin folder layout

One folder per plugin under `argus/plugins/`. Only `plugin.yaml` is required; everything else is added when the plugin needs it.

**As built (API 1.0, C10):** a plugin is `plugin.yaml` plus `plugin.py`. The workflows are Python functions in `plugin.py`, registered with `@workflow("<id>", "<name>")` from `argus.worker`; YAML workflow files come later. argusd reads the manifest (bad ones show under `GET /plugins` errors, never stop Argus), wires its triggers, and hands it to workers that have what it needs (`runs_on: desktop` needs a worker started with `--cap desktop`). The worker imports `plugin.py` and gives each job `ctx.files` (only the manifest's folders, minus `paths.blocked`; moves never overwrite; deletes go to the Recycle Bin), `ctx.http` (only listed hosts), `ctx.secrets` (only listed names), `ctx.store` (small state in argusd), `ctx.config`, `ctx.emit`, `ctx.dry_run`, and `ctx.llm` starting at the manifest's tiers. Anything outside the manifest raises `PermissionDenied` and the job goes dead. **Undo and Wrong:** every `ctx.files` change is listed under the job's Changes in Helios (Runs page). A plugin with an `undo` workflow (input `{"from", "to"}`) gets an Undo button per move; one with `helios.wrong: {workflow, label}` gets a Wrong button whose choices come from its state key `choices` (input `{"from", "value"}`), and should keep the answer as an example for its models. **Share:** a manifest `share: [{workflow, label, accepts: [file, image, pdf, url, text]}]` puts the plugin on Helios's Share page (the phone's share menu); the job input has `title`, `text`, `url`, `note` and `files` (`[{name, type, size}]`), and `ctx.shared(name)` returns a file's bytes. A new plugin runs in dry-run (changes are logged, not made) until it is listed under `plugins.live` in argus.yaml.

```python
from argus.worker import workflow

@workflow("downloads-organizer", "sort")
def sort(ctx):
    for f in ctx.files.list(ctx.config["inbox"]):
        ctx.step("move", ctx.files.move, f, ctx.config["target"])
```

```
argus/plugins/bill-filer/
  plugin.yaml          # manifest: who, what, triggers, permissions, config
  workflows/
    file-bill.yaml     # the steps (one file per workflow)
  steps.py             # Python step functions: def step(ctx, data) -> data
  checks.py            # cheap code checks run on model output
  playbooks/
    classify.md        # instructions + examples the model follows
  ui.yaml              # optional: Helios node, cards, settings layout
  tests/
    fixtures/          # sample inputs (a real CEB bill PDF, etc.)
    test_plugin.py     # runs with a mock model, no GPU needed
  README.md            # what it does, how to configure it
```

Naming: folder name = plugin `id`, lowercase with dashes. Playbooks are plain markdown so Claude's nightly review can suggest edits as diffs.

## The manifest: plugin.yaml

The manifest is the contract between a plugin and core. The loader rejects a plugin whose manifest is invalid, and at run time a plugin can do only what its manifest lists.

```yaml
id: bill-filer
name: Bill filer
version: 0.1.0
kind: workflow                  # workflow | connector | trigger
argus_api: ">=1.0 <2.0"          # plugin API versions it works with
description: Reads new PDFs, files bills, sends each bill to Cashly for approval.

runs_on: desktop                # laptop | desktop (needs the PC awake)
needs: [gpu]                    # gpu | none

triggers:
  - folder_watch:
      paths: ["~/Downloads", "~/Drop"]
      match: "*.pdf"
      stable_for: 2m
  - manual: {label: "File a bill"}

workflows: [workflows/file-bill.yaml]

permissions:
  files:
    read:  ["~/Downloads", "~/Drop"]
    write: ["~/Documents/Bills"]
    delete: none                # none | recycle_bin (never permanent)
  models: [T0, T1]              # highest tier it may use; T2/T3 only via escalation
  claude_calls_per_day: 5       # optional; default claude.plugin_calls_per_day (10); 0 = never Claude
  network: []                   # hosts it may reach, empty = none
  uses: [cashly.add_entry]      # actions from connector plugins

approvals:
  - type: entry                 # entry | batch | draft (see Helios section)
    when: always                # always | below_confidence | never

config:                         # shown as a settings form in Helios
  known_senders:
    type: list
    default: [CEB, Dialog, "Water Board"]
  reminder_days_before_due:
    type: int
    default: 3

helios:
  node: {label: Bill filer, group: files, icon: file}
```

Required keys: `id`, `name`, `version`, `kind`, `argus_api`, `triggers` (except connectors), `permissions`. Everything else has a safe default: no files, no network, T0 only, no Claude.

## What plugins can use

Plugins are built from three things core provides: triggers that start a run, step types a workflow is made of, and the `ctx` object that step functions receive.

### Triggers

| Trigger | Starts a run when | Example |
| --- | --- | --- |
| `schedule` | a cron time or the night window arrives | duplicate finder, weekly |
| `folder_watch` | a matching file is new and unchanged for `stable_for` | bill filer, screenshot renamer |
| `webhook` | an HTTP call hits `/hooks/<plugin-id>` | bank SMS forwarded from the phone |
| `event` | another plugin emits a named event | `bill.filed` starts a reminder |
| `manual` | you press its button in Helios, Tracker or Life Hub | invoice builder |

### Step types (in workflow YAML)

| Step | Does | Notes |
| --- | --- | --- |
| `code` | runs a Python function from `steps.py` | no model, fast, testable |
| `llm` | asks a model tier with a playbook and output schema | escalates on failed checks |
| `claude` | asks T3 directly | counts against the daily cap |
| `search` | queries SearXNG | needs the PC awake |
| `approve` | pauses until you approve in Helios or ntfy | type: entry, batch or draft |
| `notify` | sends an ntfy message, optional buttons |  |
| `emit` | publishes an event other plugins can react to |  |

### The ctx API (inside step functions)

```python
class BillFields(BaseModel):                                  # the answer must parse into this
    vendor: str
    amount: float
    due: str

def read_bill(ctx, data):
    text = ctx.files.read_text(data["path"], pages=1)      # only declared paths
    fields = ctx.llm(PLAYBOOK, text, schema=BillFields,       # T1 first, escalates if rejected
                     check=lambda f, src: None if f"{f.amount:,.2f}" in src
                                          else "amount not found in the bill text")
    ctx.log("parsed", vendor=fields.vendor, tier=ctx.last_answer.tier)
    return {**data, **fields.model_dump()}

def send_to_cashly(ctx, data):
    ok = ctx.approve(type="entry", title=f"{data['vendor']} bill",
                     fields=data)                            # waits for you
    if ok:
        ctx.apps.cashly.add_entry(**ok.fields)               # via the Cashly connector
        ctx.emit("bill.filed", data)
```

| `ctx` service | Use |
| --- | --- |
| `ctx.llm(playbook, input, schema=, check=, tiers=)` | model call, cheapest tier first. The reply is parsed as JSON, validated against the Pydantic `schema`, then `check(answer, input)` runs (return a reason to reject). A rejected answer is retried once with the reason, then escalated up the chain (T1 → T2 → T3) with the rejected answer as advice. Returns the answer; `ctx.last_answer` has the tier, attempts and trail. Built in Argus 0.5 (C7). |
| **When the local models can't** | for every plugin: if every tier `ctx.llm` tried fails and Claude wasn't among them (for example `tiers=["V1"]`), Claude gets one try with the same input, pictures and the local models' rejected answers, counted against the plugin's `claude_calls_per_day` (default `claude.plugin_calls_per_day`, 10) and the global cap. `claude_last=False` turns that off for one call (to try a cheaper route first). If Claude fails too, `EscalationExhausted`: skip the item or ask you with `ctx.ask_me`. `ctx.local_tiers()` is the chain without Claude. |
| `ctx.claude(prompt, input, schema=, check=, images=, advice=)` | direct Claude call (tools off, one turn; it sees `images` too), counted against the plugin's and the global daily cap. |
| `ctx.ask_me(title, fields, summary=, image=)` | the last resort: ask you to fill in `fields` (Helios and the phone; `image` is a small picture to decide by). Returns the fields as you approved them, or None if you rejected it. The job waits meanwhile, like `ctx.approve`; call it inside `ctx.step`. |
| `ctx.files` | read, write, move, `recycle` inside declared paths only |
| `ctx.approve(type, title, fields)` | ask you and park the job (no worker held) until you answer on the phone or in Helios; the step then runs again and gets a Decision: truthy when approved, .fields = the values as approved (edits included), .state = approved, rejected or expired. Types: entry (editable fields), batch (items; Argus adds up count and total), draft (summary + link). Call it inside ctx.step: a retried step gets the same approval back, never a second one. Built in Argus 0.6 (C8). |
| `ctx.notify(title, text, priority=, tags=, link=)` | phone message through the outbox (ntfy): sent once even if the step runs again, retried if ntfy is down. Built in 0.6 (C8). |
| `ctx.emit(name, data)` / `ctx.log(msg, **kv)` | events and log lines |
| `ctx.store` | the plugin's own small table (state between runs) |
| `ctx.secrets["name"]` | tokens from the encrypted `.env`, never in files |
| `ctx.config` | the plugin's settings from Helios |
| `ctx.apps.<connector>.<action>` | actions offered by connector plugins |

Step functions must be idempotent (safe to run twice) and must never call Ollama, Claude or the filesystem directly.

## Rules every plugin follows

Core enforces these; a plugin that breaks one fails its run and shows red in Helios.

1. **Only what the manifest declares.** Files outside declared paths, undeclared hosts, or a higher model tier are refused.
2. **No permanent deletes.** `delete` is `none` or `recycle_bin`. Anything removed is restorable for 30 days.
3. **Money always needs you.** Any action that writes an amount (Cashly entries, invoices) goes through `ctx.approve`. Totals are calculated in code, never by a model.
4. **Dry-run first.** A new or updated plugin runs in dry-run until you promote it. Dry-run shows what it would do, and does nothing.
5. **Every change can be undone.** File moves and renames go into the undo log; Helios shows an Undo button per run.
6. **Budgets.** Claude calls count against the plugin's `claude_calls_per_day` and the global cap of 30. Each step has a timeout (default 2 minutes).
7. **Secrets stay in `.env`.** Never in manifests, playbooks, logs or events.
8. **Office data stays out.** No plugin reads DirectFN or work folders or repos.
9. **One GPU job at a time.** Plugins never load models themselves; the GPU queue schedules them.

Security rules for plugin authors:

- **Treat every input as untrusted.** File contents, SMS, emails and web pages may contain text that tries to steer the model. Pass them to `ctx.llm` as data with a schema, and act only on validated fields.
- **Never build commands, paths or queries from model output** without checking them against an allowlist.
- **Plugins run in their own process as a low-privilege user.** Don't rely on anything outside `ctx`.
- **Webhook triggers must verify the request signature** (`ctx.verify_signature`) before doing anything.
- **Log decisions, not secrets or full documents.** Amounts and names are fine; account numbers are not.

## Helios integration

A plugin shows up in Helios with no front-end code. Helios builds everything from the manifest and the events the plugin emits.

- **Map node:** from `helios.node` (label, group, icon). Lines are drawn from the connections it actually uses: models, apps, files.
- **Settings form:** generated from `config` (list, int, text, bool, choice, path). Changes are saved to the plugin's config file in Git.
- **Runs, timeline, messages:** from the event log, same as every other plugin.
- **Approval cards:** one of three built-in types, so every approval looks and works the same.

| Card type | For | Shows | Buttons |
| --- | --- | --- | --- |
| `entry` | one item to confirm or edit | source, parsed fields, confidence, editable fields | Approve, Reject |
| `batch` | many items, one decision | count, total size or value, top items, full list on Open | Approve all, Open, Not now |
| `draft` | a document to review | title, summary lines, link to the file | Approve, Open, Not now |

The same card goes to ntfy as a notification with the same buttons.

If a plugin really needs its own panel (Cashly's month summary, for example), it adds a `ui.yaml` using Helios's built-in widgets: stat, list, table, bar, and link button. Custom React panels are a last resort.

## Lifecycle and testing

Every plugin moves through the same five stages; Helios shows the stage on the plugin's node.

1. **Scaffold:** `argus plugin new bill-filer --kind workflow` creates the folder with a filled-in manifest, one workflow, a step, a playbook and a test.
2. **Test:** `argus plugin test bill-filer` validates the manifest, then runs the workflow on `tests/fixtures/` with a mock model. No GPU or PC needed, so it runs on the laptop.
3. **Dry-run:** `argus plugin enable bill-filer` turns it on in dry-run. It runs on real triggers and shows what it would do in Helios.
4. **Live:** after a few good dry-runs, press **Go live** in Helios, or run `argus plugin live bill-filer`.
5. **Update or roll back:** plugins live in Git. A changed plugin goes back to dry-run automatically. `argus plugin rollback bill-filer` restores the last live version.

Test set: every run you mark correct in Helios is saved to `tests/eval/`. Before a playbook change or model swap goes live, the plugin must score at least as well on that set as the current version.

Hot reload: core picks up changed plugins without a restart. Disabling a plugin stops new runs and lets running ones finish.

## Checklist for adding a new plugin

Copy this list into the plugin's README and tick it off.

- [ ] Pick the kind: workflow, connector or trigger. If it wraps one of your apps, it is a connector.
- [ ] `argus plugin new <id>` to scaffold the folder.
- [ ] Fill `plugin.yaml`: triggers, the smallest permissions that work, approvals, config with defaults.
- [ ] Write the workflow YAML: prefer `code` steps; use `llm` only where judgement is needed.
- [ ] Write the playbook: rules, allowed outputs, 3–5 examples, output schema.
- [ ] Add checks for every model output (valid JSON, allowed values, amount present in source).
- [ ] Add 5+ real fixtures and run `argus plugin test <id>`.
- [ ] Enable in dry-run and watch a few runs in Helios.
- [ ] Go live.
- [ ] Add a row to the catalogue below.
- [ ] If core needed a new `ctx` service, add a changelog entry below.

## Plugin catalogue

One row per plugin, in build order: wave 1 first. Each wave uses what the one before built (folder triggers, then the Cashly connector, then schedules). Update the Status column as plugins move stage.

| Wave | Plugin | What it does | Runs on | Models | Needs you | Status |
| --- | --- | --- | --- | --- | --- | --- |
| 1 Files | downloads-organizer | sorts Downloads as files land, undo in Helios | desktop | T0, T1 | moves in dry-run first | port from existing script (first plugin) |
| 1 Files | bill-filer | new PDF bill: vendor, amount, due date; renames and files it; reminder before due | desktop | T0, T1 | entry per bill | planned |
| 1 Files | screenshot-renamer | gives screenshots descriptive names | desktop | T1 (+ vision fallback) | no, undo available | planned |
| 1 Files | duplicate-finder | weekly duplicates, one batch approval, Recycle Bin | desktop | none | batch | planned |
| 2 Money | cashly | connector to Cashly | laptop | none | n/a | planned |
| 2 Money | bank-sms | forwarded bank SMS to a categorised Cashly entry, amount read by code | laptop, GPU for T1 | T0, T1 | entry below 0.80 confidence | planned |
| 2 Money | receipt-capture | receipt photo from the phone to a Cashly entry plus the stored photo | laptop, PC for OCR | T1 | entry | planned |
| 2 Money | invoice-builder | monthly draft invoice PDF from hours and closed issues; totals in code | laptop, GPU for T2 | T2 | draft | planned (needs hours per client in Tracker) |
| 3 Day | tracker | connector to the issue tracker | laptop | none | n/a | planned |
| 3 Day | morning-brief | 7 am notification: spending, bills due, failed jobs, digest highlights | laptop | T1 | no | planned |
| 3 Day | daily-digest | SearXNG + Discord digest, retried on failure, visible in Helios | desktop | T1, T2 | no | port from existing script |
| 3 Day | voice-to-task | phone voice note, Whisper on the PC, task in the tracker | desktop | T1 | no | planned |
| 3 Day | weekly-review | Sunday plan: what slipped, top 3 next week, spending notes | laptop | T3 | no | planned |
| 4 Lab | model-scout | new Ollama models that fit 12 GB, run on your test sets overnight | desktop | T1, T2 | no | planned |
| 4 Lab | backup-checker | nightly backup of the Argus database and apps to the PC, restore proven | laptop | none | no | planned |
| 4 Lab | code-review | T2 first pass on PRs; Claude only on tagged PRs | laptop, GPU for T2 | T2, T3 | no | planned |
| 4 Lab | llmclip | clipboard hotkey actions run through Argus's models | desktop | T1, T2 | no | planned |
| 4 Lab | research-agent | nightly research runs, Claude advises | desktop | T1, T2, T3 | review of accepted features | port from existing scaffold |
| 5 Later | paper-radar | weekly new arXiv papers on deepfake detection and audio-video work, 3-line summaries | desktop | T1, T2 | no | planned |
| 5 Later | ctf-coach | one picoCTF or crackmes challenge a week at your level, hints without spoilers | laptop | T2, T3 | no | planned |
| 5 Later | docker-health | checks Docker Desktop, WSL and containers; says what failed | desktop | T1 | no | planned |
| 5 Later | power-cut-resume | after a power cut: what was interrupted and what resumed, in one message | laptop | none | no | planned |
| 5 Later | utility-tracker | CEB and water bills over time; flags a jump over 30% | laptop | T0, T1 | no | planned (reuses bill-filer) |
| 5 Later | budget-nudge | mid-month alert when a category runs ahead of budget; maths in code | laptop | T1 (wording only) | no | planned (needs cashly) |

## Plugin API changelog

The plugin API version is what `argus_api` in each manifest refers to. Minor versions only add things; a major version can break plugins and lists what to change.

| Version | Date | Change |
| --- | --- | --- |
| 1.0 (draft) | 2026-09-28 | ctx.llm and ctx.claude built (Argus C7): playbook, input, Pydantic schema, check function, optional tier chain; the tier is read from ctx.last\_answer instead of being passed in. |
| 1.0 (draft) | 2026-09-28 | First design: manifest, 5 triggers, 7 step types, `ctx` services, 3 approval card types. Not built yet. |

## Document history

| Version | Date | Change |
| --- | --- | --- |
| 0.5 | 2026-09-29 | Plugin catalogue in five waves (files, money, day, lab, later): 24 plugins with what each does |
| 0.4 | 2026-09-28 | ctx.approve (Decision, three card types) and ctx.notify match the built API (C8) |
| 0.3 | 2026-09-28 | ctx.llm and ctx.claude match the built API (schema + check, escalation, last\_answer) |
| 0.2 | 2026-09-28 | Security rules for plugin authors |
| 0.1 | 2026-09-28 | Created (D6) |
