# Docs

Markdown copies of the Argus design documents, versioned with the code. The living versions are the Claude
Docs (links below); Claude refreshes this folder whenever a document changes, in the same pull request as the
code it describes, so `git log docs/` is the documents' history.

| Document | File | Version | Living doc |
| --- | --- | --- | --- |
| LLM Orchestrator Plan | [orchestrator-plan.md](orchestrator-plan.md) | 0.8 | [open](https://claude.ai/code/artifact/20b291cf-7613-40ff-9809-bb61b9c06f97) |
| Argus Development Plan | [development-plan.md](development-plan.md) | 1.2 | [open](https://claude.ai/code/artifact/72519796-0945-4759-8a45-7d2104891dac) |
| Argus Core Design | [core-design.md](core-design.md) | 0.6 | [open](https://claude.ai/code/artifact/7637bb19-994d-444e-b1d0-917200ca5a90) |
| Argus Plugin Guide | [plugin-guide.md](plugin-guide.md) | 0.3 | [open](https://claude.ai/code/artifact/0ea86799-3237-48c4-8c5c-edf08c48c9a5) |
| Argus Docs Index | [docs-index.md](docs-index.md) | 1.0 | [open](https://claude.ai/code/artifact/1985f70d-4ddb-4252-b779-7caca2a41798) |
| Helios mockup | (interactive page) | 0.4 | [open](https://claude.ai/artifact/EAhWvwckWzyc6ouyPdfW6C) |

Exported 2026-09-28. Diagrams (D1 to D10) are live drawings inside the Claude Docs, so the Markdown shows a
placeholder line with the diagram's ID and version where each one sits; `diagrams/` will hold SVG copies.

**Versions:** documents use `MAJOR.MINOR` (minor = a section or decision changed, major = approved for
building or a reversed decision). Each document ends with its own history table, and the Docs Index keeps
the change log across all of them.
