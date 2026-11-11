# Roadmap

What gets built next, in order. Each phase is its own branch; the full working plan (with status) is kept in the
Home Lab project as `argus-roadmap.md`. Checked on 2026-11-11 against the code.

1. **Helios usability**: Plugins page with per-plugin controls (done); modern terminal look with a full-width map,
   inspector drawer, events dock and smooth motion (done); one shared kit on every page, a home page that answers
   "is everything fine?", the Inbox, Settings and Models pages (done); a cleaner phone view on the same kit (next).
2. **Guidance loop** (done): samples, verdicts, nightly lessons with test scores; "Try with another model" on a kept
   answer; a Learning tab per playbook with its tests, "Run tests now" and the pass-rate history; lessons that pass
   fewer tests than the playbook does now are not offered.
3. **Tracker / Life Hub connector** (done): Ari lists, shows, adds, moves, edits and comments on issues, lists
   projects and runs the timer; overdue issues reach you hourly in the day.
4. **Research ports**: the daily digest and the structural-break research agent as plugins (their source isn't in
   this repo yet; it has to come over first).
5. **Money**: Cashly through Life Hub, the money advisor with price watches, weekly rates, the Sunday review and the
   monthly PDF (done); still to do: the bill filer, bank SMS from the phone to a signed webhook, and a money panel in
   Helios (or a link to Life Hub's, which already shows it).
6. **Laptop move** (done): deploy/README.md and deploy/linux; the laptop runs Argus, the PC is the GPU worker.

Later, when their absence is felt (from the development plan's "Grow" list): the invoice builder once the Tracker
records hours per client, Uptime Kuma or LiteLLM, passkey login and an encrypted secrets file, plugin process
isolation.
