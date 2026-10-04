# Ticket fixer: Tracker + Argus + Ari (plan)

Version 0.7 (2026-10-03; Helios tab built)

Goal: manage Tracker tickets from Ari, and let a ticket in a chosen project be fixed by an LLM working in
that project's folder, in two phases, **plan** then **execute**, with a separate model for each phase.
The plan is attached to the ticket as a document. Execution waits for your approval, and the fix lands on a
branch that you review and merge.

## What exists today

- **Tracker** (`G:\Projects\tracker`, FastAPI + React, Docker, `X-API-Key`). It has projects (key, name,
  kind, url), tickets (status backlog/todo/in_progress/review/done, priority, labels, checklist), comments,
  attachments (upload/download), activity, a timer and a summary. It has no webhooks and no
  "repo path" on projects.
- **Argus `tracker` plugin**: Ari tools `work_summary`, `list_issues`, `add_issue`, `move_issue` and
  `issue_timer`, plus an hourly `check` for overdue tickets.
- **Argus** already has jobs, approvals with phone buttons, the phone inbox, Helios and the Claude CLI
  provider. That provider always runs with tools off, and that is a standing security rule.

## The flow

```
ticket in a mapped project, label "ai-fix" (or Ari: "fix ACME-12")
  -> PLAN   (plan model, read-only in the project folder)
       -> ACME-12-fix-plan.md attached + comment with the summary, label "plan-ready"
       -> approval on the phone / Helios: "Run the fix for ACME-12 with <fix model>?"   [Approve] [Reject]
  -> EXECUTE (fix model, in a git worktree on fix/acme-12-<slug>, edits + the project's test command)
       -> one commit on the branch (change-flow message), ACME-12-fix-report.md attached,
          ticket -> review, notification
  -> you: review the branch, then your usual pr / merge (Claude never pushes or merges)
```

Reject, a failed plan or failed tests leave a comment on the ticket saying why, and the ticket stays
where it was.

## Part 1: Tracker connector (manage tickets from Ari)

New Ari tools in the `tracker` plugin:

| Tool | What it does | Risky (asks first) |
|---|---|---|
| `show_issue` | One ticket: description, checklist, latest comments, attachments | no |
| `list_projects` | Project keys and names, and which ones have a folder for fixing | no |
| `comment_issue` | Add a comment | yes |
| `edit_issue` | Change title, priority, due date or labels | yes |
| existing | summary, list, add, move, timer | as today |

Direct paths (no model step): "what's urgent at work", "show ACME-12", "move ACME-12 to done".

## Part 2: Project mapping and models (Argus config)

The folder for each project lives in Argus, not Tracker, because it is a PC fact and it is the allowlist:

```yaml
fixer:
  enabled: true
  plan_model: claude:opus        # default per phase
  fix_model: claude:sonnet
  max_minutes: {plan: 10, fix: 30}
  fixes_per_day: 12
  projects:
    ACME:
      path: G:\Projects\acme
      test: npm test             # run after the fix; must pass for the commit
      auto_plan: true            # plan as soon as a ticket gets "ai-fix" (no approval needed to plan)
      fix_model: claude:opus     # per-project override
```

**Picking models**, from most to least specific:

1. Ari: "fix ACME-12, plan with opus, fix with sonnet".
2. Ticket labels: `plan:opus`, `fix:sonnet`.
3. Project setting.
4. Default.

Helios has dropdowns for each.

Model choices:

- `claude:opus`, `claude:sonnet` and `claude:haiku` run through the Claude Code CLI with your login.
- `ollama:<model>` runs a local model (for example qwen2.5-coder) through the same CLI pointed at
  Ollama's Anthropic-compatible endpoint. This still needs a spike to confirm it works. Local models
  are fine for planning; for executing, expect weaker results.

## Part 3: The two phases (new `fixer` plugin, runs on the PC worker)

**Plan.** Claude Code runs in the project folder in plan mode with **read-only tools** (Read, Grep, Glob). It
has no edits, no shell, no web, and reading `.env*` is denied. It gets the ticket (title, description,
checklist, comments, existing attachments) and writes the plan with these sections: summary, root cause,
files to change, steps, tests to add or run, risks, out of scope. Argus checks the sections in code,
uploads the plan as `ACME-12-fix-plan.md`, comments with the summary, then asks you to approve the fix.
To change the plan, upload an edited `ACME-12-fix-plan.md`; execution always uses the newest one.

**Execute** (only after you approve). These steps run in order:

1. Make a fresh worktree from the project's main branch on a new branch, `fix/acme-12-<slug>`.
2. Run Claude Code there with edit tools, limited to that folder. Bash is limited to the project's test
   command and read-only git. There is no push and no network.
3. Run the test command.
4. If the tests pass: make one commit with a change-flow message (`fix(acme): …` plus What / Why /
   How tested / Risk), attach `ACME-12-fix-report.md` (files changed, diff stat, test output, anything
   it couldn't do), move the ticket to **review** and notify you.
5. If the tests fail: attach the report and the failure, keep the branch for you to look at, and leave
   the ticket in progress.

**Proof** (required before a ticket can move to review). The fix only counts with evidence. Every fix run
gets a proof folder outside the repo, `<worktree>/../proof/ACME-12/`, so nothing in it can be committed.

What the fix model is told (in its prompt) to save there, with exact names:

| File | What |
|---|---|
| `ACME-12-before-<what>.png` | Screenshot of the problem on `main` before the change (UI tickets) |
| `ACME-12-after-<what>.png` | The same view after the fix |
| `ACME-12-<what>.log` | Any extra output that shows the fix (a command, a request and its response) |
| `proof.md` | The proof checklist: each plan step, what was done, and the evidence file or test name for it; then "how to check it yourself" |

`<what>` is a short lower-case name, like `board` or `login-dialog`. With a **Claude** fix model, Claude
starts the app, takes the screenshots and writes the files itself. Its tools allow the project's start
command and a headless browser screenshot command, only for `localhost`. A local model that can't
manage this falls back to Argus taking `proof.pages` screenshots itself (configured per project).

What Argus adds itself, because it shouldn't rest on the model's word:

- **Tests:** Argus runs the project's test command on the branch and on `main` (before and after) and
  saves the output as `ACME-12-tests.log`.
- **Diff:** the branch's diff as `ACME-12-fix.diff`.
- **Session log:** the model's tool calls, with secrets removed, as `ACME-12-session.log`.

Argus then checks the proof in code. File names must follow the pattern; only png/jpg/log/txt/md are
accepted, each up to 10 MB. The tests must pass, every plan step needs a line in `proof.md`, and UI tickets
(label `ui`, or the plan says so) need at least one before/after pair. Argus uploads everything to the
ticket and links it in `ACME-12-fix-report.md`. With proof missing, the ticket stays in progress with a
comment saying what's missing. Project config gets:

```yaml
      proof:
        start: docker compose up -d --build   # how the model (or the fallback) starts the app
        url: http://127.0.0.1:8080
        pages: ["/", "/board"]                # only used by the fallback for non-Claude models
```

**Limits.** A time budget per phase (enforced by killing the process), one fix at a time per project,
`fixes_per_day`, and a cancel from Ari or Helios. Every run is logged as job steps, so Helios shows what
happened.

**Trigger.** Tracker has no webhooks. Phase 1 polls mapped projects for `ai-fix` tickets every 2 minutes,
which is cheap since there is one API call per check. Optionally, later, a small Tracker change can call
Argus on ticket create and label changes.

## Part 4: Ari skill ("fix tickets")

Ari tools:

- `fix_ticket(key, plan_model?, fix_model?)` plans now (risky: asks first).
- `fix_status(key?)` reports the phase, the models and the last line of the log.
- `run_fix(key)` approves the execute phase. It is the same approval the phone shows.
- `cancel_fix(key)` stops a running fix.

Example lines:

- "Fix ACME-12."
- "Plan ACME-12 with opus and fix it with sonnet."
- "How's the ACME-12 fix going?"
- "Go ahead with the fix."
- "Cancel it."

Direct paths cover "fix <KEY>" and "status of the <KEY> fix". The playbook tells Ari to read the plan
summary aloud before asking to run it.

## Part 5: Helios

A **Fixer** page lists fixes (ticket, phase, models, time, state) with links to the plan and the report,
Approve / Cancel buttons, and per-ticket model dropdowns. The Settings page edits project folders, test
commands and default models.

## Security (an exception to a standing rule, needs your OK)

Today Claude runs only with tools off. The fixer needs tools, so the exception is scoped:

- It runs only in `fixer`, only in folders listed under `fixer.projects` (the allowlist), and only on
  the PC worker.
- Plan is read-only, and execution waits for your approval every time.
- Execution happens in a separate worktree on a new branch. It never pushes, merges or touches main.
- Its Bash commands are limited to the test command and read-only git. It has no web tools, and
  `.env*` and secrets are denied.
- Time and daily limits apply, and every run is logged.
- DirectFN or office repos are never mapped (the existing rule).

## Delivery (one package per step, using the change flow)

1. `feat(tracker)`: show/comment/edit/projects tools, direct paths, tests with a fake Tracker.
2. `feat(fixer)`: config, project allowlist, model picking, the plan phase, attaching the plan, approval.
3. `feat(fixer)`: the execute phase (worktree, limited tools, tests, commit, report, ticket to review) and proof
   (Claude saves named screenshots and proof.md in the proof folder; Argus adds tests, diff and session log,
   checks it all in code and attaches it to the ticket).
4. `feat(ari)`: the fix-ticket tools, direct paths, playbook.
5. `feat(helios)`: the Fixer page and settings.
6. A spike: local models through Ollama's Anthropic endpoint (keep it only if it works).

## Decisions (2026-10-03)

- Security exception: approved, scoped exactly as in "Security" above.
- Trigger: planning starts when a ticket gets the `ai-fix` label (or Ari is asked). Execution always waits
  for approval.
- First mapped project: Tracker itself (`G:\Projects\tracker`). Its test command is still to be confirmed.
- The project to folder mapping stays in Argus config, not in Tracker.

## Status

- Step 1 (Tracker tools for Ari): done.
- Step 2 (plan phase): done. Labels: `ai-fix` starts it; `ai-planning`, `plan-ready`, `plan-rejected`,
  `plan-failed`, `fix-approved` show where it is. The plugin's `projects` setting is the allowlist
  (`KEY | folder | test command | plan model | fix model`).
- Step 3 (execute phase and proof): built, tested with a real git repo and a fake Claude. The scan runs a ticket
  labelled `fix-approved` (or Ari's `run_fix`): worktree and branch `fix/key-slug` from main, test command before,
  Claude (stream-json, `acceptEdits`, tools limited by `fix_allow`, deny list for commit/push/web/.env), what
  Claude changed is staged (build output from the test command is left out), forbidden files (.env, keys,
  `.github/workflows`, `.git`) are reverted and fail the run, the test command runs again, the diff is saved, and
  the proof is checked in code (names, size, `proof.md` lines against plan steps, a before/after pair when the
  ticket has the label `ui` or the plan asks for screenshots, tests passing). Success: one commit with a
  change-flow message (`fix(trk): TRK-5 title`; What/Why/How tested/Risk from the plan), proof and
  `KEY-fix-report.md` attached, label `fix-done`, ticket to review, phone message. Failure: wip commit on the
  kept branch, what exists is attached, label `fix-failed`, ticket stays in progress.
  Labels added: `ai-fixing`, `fix-done`, `fix-failed`.
- Step 4 (Ari): `fix_ticket` (asks first) only queues: it adds the label `ai-fix` (and `plan:`/`fix:` model labels)
  and answers at once; `run_fix` (asks first) adds `fix-approved` once a plan is attached; `fix_status` reads the
  labels (one ticket or all). Direct paths need no model: "fix ACME-12", "plan ACME-12 with opus and fix with
  sonnet", "run the fix for ACME-12", "how's the ACME-12 fix going?", "what's being fixed?". After your yes,
  Ari answers from the result. The playbook tells Ari to read the plan summary before running a fix.
  `cancel_fix` is not built yet. Jobs: `scan` (every two minutes) does the work itself, one at a time, and never
  waits for you; `ask` (every two minutes) is the separate job that waits for your phone/Helios answer, so an
  unanswered question can't block planning or fixing (an unanswered one is simply left, and Ari or the label can
  still approve it). A limit or a run in progress leaves the ticket queued for the next scan; any other failure
  before the run starts is shown on the ticket (`plan-failed` / `fix-failed` plus a comment).
- Step 5 (Helios): the Ticket fixer's page (Plugins > Ticket fixer) has a **Fixes** tab: every ticket that is in the
  flow with its stage, a "Plan" or "Run the fix" button that fits the stage (the same `queue` / `approve`
  workflows Ari uses), model dropdowns for the plan and fix phases (blank = the ticket label, project line or
  default), a box to start a plan for any ticket, and a link to Tracker. Project folders, test commands and the
  default models are in the page's Settings tab. Plan approvals stay in the Inbox. Cancel is not built.
- Differences from the plan above: the allowed commands are the `fix_allow` list (default npm run/test/ci,
  npx playwright/vitest, node, pytest, read-only git), not just the test command, because Claude needs to start
  the app and take screenshots; the Argus-side screenshot fallback for non-Claude models is not built. A
  project's test command must be self-contained (include `npm ci`), because it runs in a fresh worktree.
- Not yet tried with the real `claude` command and Tracker (the tests use fakes). The flags passed
  (`--permission-mode acceptEdits`, `--allowedTools`, `--disallowedTools`, `--add-dir`, `--output-format
  stream-json --verbose`) and the `Bash(cmd *)` pattern form are from the Claude Code documentation as I know
  it and need one real run to confirm.
- Tracker's test command for the first project is `cd web && npm ci && npm run build` (Tracker has no tests yet).
