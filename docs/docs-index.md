# Argus Docs Index

Sep 28, 2026 · @Sasanka

## Versioning rules

Every Argus document and diagram carries a version number, and every change to one is logged. This index is the single place to see what is current.

**Documents** use `MAJOR.MINOR`:

- **Minor** (0.4 → 0.5): a section added or changed, a decision recorded, a diagram updated.
- **Major** (0.9 → 1.0): the document is approved as the basis for building, or a change reverses an earlier decision (for example a new architecture).
- Documents stay below 1.0 while they are drafts. A document reaches 1.0 when its milestone starts being built.

**Diagrams** have an ID (`D1`, `D2`, …) that never changes, and a version (`v1`, `v2`, …) that goes up whenever the picture changes. The ID and version appear in the diagram's caption, for example "D7 v4 · roadmap".

**Every change** gets one line in the document's own history (version, date, what changed), and in the change log at the end of this index. A change that touches several documents gets one line in each.

## Documents

| Document | Version | Status | Covers |
| --- | --- | --- | --- |
| LLM Orchestrator Plan | 0.8 | draft, the long-term picture | why Argus exists, tiers, Helios design, automations, apps |
| Argus Development Plan | 1.2 | the working tracker | milestones, tasks, gates, architecture, security, Helios UI |
| Argus Core Design | 0.6 | C1–C7 built; becomes 1.0 when M1 completes | argusd, job lifecycle, data model, stability, build steps |
| Argus Plugin Guide | 0.3 | draft (plugin API 1.0 draft) | how to write and add plugins |
| [Helios mockup](https://claude.ai/artifact/EAhWvwckWzyc6ouyPdfW6C) | 0.4 | interactive design, simulated data | the dashboard to build in M1–M3 |
| Argus code (`G:\Projects\argus`) | 0.4.0 | tag `v0.4.0` | core steps C1–C6: foundation, jobs, workers, live events, Helios live map |

## Diagrams

| ID | Diagram | Version | Lives in | Last change |
| --- | --- | --- | --- | --- |
| D1 | Early architecture (laptop, desktop, tools) | v2 | LLM Orchestrator Plan | renamed Conductor to Argus, dashboard to Helios |
| D2 | Guidance loop (playbook, escalation, review) | v1 | LLM Orchestrator Plan | first version |
| D3 | Build phases (original 4 phases) | v2 | LLM Orchestrator Plan | added ntfy, Kuma, LiteLLM, Open WebUI; superseded by D7 |
| D4 | PC power cycle (wake, work, shut down) | v1 | LLM Orchestrator Plan | first version |
| D5 | Helios layout sketch | v1 | LLM Orchestrator Plan | first version; the Helios mockup is the current design |
| D6 | How a plugin connects | v1 | Argus Plugin Guide | first version |
| D7 | Roadmap | v4 | Argus Development Plan | M1 now 3 weeks; later milestones shifted 1 week |
| D8 | System architecture after MD | v2 | Argus Development Plan | widened file box |
| D9 | Core process architecture | v1 | Argus Core Design | first version |
| D10 | Job state machine | v1 | Argus Core Design | first version |

D7 history: v1 7 milestones over 10 weeks · v2 build on the PC, deploy later (MD) · v3 lean first, then grow · v4 M1 extended to 3 weeks.

## Git, releases and updates

Once the repo exists (M0), Git holds the permanent history of both the docs and the code, and one command each pushes a new version out.

### Docs and diagrams in Git

- `docs/` in the repo holds each document as Markdown and each diagram as an SVG file named by ID (`docs/diagrams/D7-roadmap.svg`).
- `argus docs sync` exports the current Claude Docs versions into `docs/`, updates `docs/CHANGELOG.md`, and commits with the message `docs: <doc> v0.9`.
- Each document release is also tagged (`docs-dev-plan-v0.9`), so any past version can be opened with one Git command.
- The Claude Docs stay the easy place to read and comment; Git is the record.

### Versions of Argus itself

| Thing | Version scheme | Where it shows |
| --- | --- | --- |
| Argus core | `MAJOR.MINOR.PATCH` (patch = fix, minor = new feature, major = breaking change) | Helios footer, `/health`, map tooltip |
| Plugin API | `MAJOR.MINOR` (in the plugin guide) | each plugin's `argus_api` range |
| Each plugin | its own `version` in `plugin.yaml` | Helios plugin details |
| Helios | follows the core version | footer |
| Database schema | migration number | `/health` |

### Pushing updates: one command each

- **`argus release patch|minor|major`** bumps the version, writes the changelog entry from the commits since the last release, tags the commit, and builds the Docker images.
- **`argus deploy`** (after MD) sends the new version to the laptop:
  - backs up the database;
  - pulls the new images;
  - runs migrations;
  - restarts `argusd`, waiting for jobs to reach a checkpoint first;
  - checks `/health`.

  If the health check fails, it rolls back to the previous version and database backup automatically, and you get an ntfy message either way.
- **`argus rollback`** returns to the previous release by hand.
- **Plugins update without a restart:** change the folder, bump its version, and core hot-reloads it into dry-run until you press Go live.
- **Helios shows it:** a small "updated to 1.3.0" note with the changelog, and the map marks parts that changed.

## Change log

Newest first. One line per change, across all documents.

| Date | Document | Version | Change |
| --- | --- | --- | --- |
| 2026-09-28 | Argus code | 0.5.0 (branch) | C7 models: ctx.llm with checks and escalation, Ollama and claude -p providers, circuit breakers, daily Claude cap, model boxes and escalation lines in Helios; 157 tests |
| 2026-09-28 | Core Design | 0.6 | C7 built |
| 2026-09-28 | Plugin Guide | 0.3 | ctx.llm and ctx.claude match the built API |
| 2026-09-28 | Argus code | Unreleased | Version control: GitHub connection, branch/PR/merge workflow, one-command releases, pre-push hook protecting main, all five documents exported to docs/ in the repo |
| 2026-09-28 | Development Plan | 1.2 | Repo host decided (GitHub, private); version control and releases done; M0 progress |
| 2026-09-28 | Argus code | 0.4.0 | C6 Helios v0: live map (React Flow + ELK) that grows by itself, message pulses, inspector for boxes/lines/jobs, events panel, phone layout; 135 tests; tag v0.4.0 |
| 2026-09-28 | Core Design | 0.5 | C6 marked done |
| 2026-09-28 | Argus code | 0.3.0 | C5 live events: WebSocket stream with replay, growing map (edges), /map /events /status, live home page, argus-events; 133 tests; tag v0.3.0 |
| 2026-09-28 | Core Design | 0.4 | C5 marked done |
| 2026-09-28 | Argus code | 0.2.0 | C4 workers: worker protocol with token auth, argus-worker with checkpointed steps, demo plugin, worker registry, writer-thread fix; 118 tests; tag v0.2.0 |
| 2026-09-28 | Core Design | 0.3 | C4 marked done |
| 2026-09-28 | Argus code | 0.1.0 | First code: C1 skeleton, C2 store, C3 jobs; 104 tests; tagged v0.1.0 in G:\\Projects\\argus. Patch 0.1.1 the same day: home page at / and cleaner logs (tag v0.1.1) |
| 2026-09-28 | Development Plan | 1.1 | Repo created; progress note under M0 |
| 2026-09-28 | Core Design | 0.2 | C1–C3 marked done |
| 2026-09-28 | Docs Index | 1.0 | Created: versioning rules, document and diagram registers, Git and release process |
| 2026-09-28 | Development Plan | 1.0 | Versioning added; docs sync, release, deploy and rollback tasks |
| 2026-09-28 | Development Plan | 0.9 | M1 extended to 3 weeks and linked to the core design; D7 v4 |
| 2026-09-28 | Core Design | 0.1 | Created: goals, process architecture (D9), job lifecycle (D10), data model, stability, scale, smoothness, modules, testing, build steps C1–C12 |
| 2026-09-28 | Development Plan | 0.8 | Share to Argus, wrong button, Ask Argus, MCP server, time saved, quiet by default |
| 2026-09-28 | Development Plan | 0.7 | Lean first, then grow; live map from M1; security dialed down; D7 v3 |
| 2026-09-28 | Development Plan | 0.6 | System architecture diagram (D8) |
| 2026-09-28 | Development Plan | 0.5 | Helios UI section with mockup screenshots and design tokens |
| 2026-09-28 | Development Plan | 0.4 | Security section |
| 2026-09-28 | Development Plan | 0.3 | Build on the PC, deploy to the laptop later (MD); D7 v2 |
| 2026-09-28 | Development Plan | 0.2 | Backups to the PC, last 5 kept |
| 2026-09-28 | Development Plan | 0.1 | Created: 7 milestones, repo layout, stack, risks (D7 v1) |
| 2026-09-28 | Plugin Guide | 0.2 | Security rules for plugin authors |
| 2026-09-28 | Plugin Guide | 0.1 | Created: manifest, ctx API, rules, Helios integration, lifecycle, catalogue (D6) |
| 2026-09-28 | Orchestrator Plan | 0.8 | File and money tools; link to the plugin guide |
| 2026-09-28 | Orchestrator Plan | 0.7 | Helios dashboard section (D5), connected apps, full Cashly view with approvals |
| 2026-09-28 | Orchestrator Plan | 0.6 | Renamed Conductor to Argus (D1 v2, D3 v2) |
| 2026-09-28 | Orchestrator Plan | 0.5 | More automations (round 2) and Argus features |
| 2026-09-28 | Orchestrator Plan | 0.4 | PC shuts down instead of sleeping (D4); automations round 1 |
| 2026-09-28 | Orchestrator Plan | 0.3 | Supporting tools: ntfy, Uptime Kuma, LiteLLM, Open WebUI |
| 2026-09-28 | Orchestrator Plan | 0.2 | Laptop becomes the always-on server; Tailscale |
| 2026-09-28 | Orchestrator Plan | 0.1 | Created: summary, tiers, guidance loop (D2), phases (D3), architecture (D1) |
