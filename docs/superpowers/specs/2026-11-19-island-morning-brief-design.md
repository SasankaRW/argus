# Morning briefing on the island (design)

Sas asked on 2026-11-18 for the weather, today's calendar, overnight jobs and anything waiting for approval, shown once on the island when they sit down. Sas was asleep, so the calls below are Claude's. The visible change waits for Sas's preview approval.

## What exists
- **`core/argus/daily.py`** already builds the morning brief.
  - `brief_parts()` returns: weather text, jobs done overnight (12 h), dead jobs, pending approvals, today's schedules, last backup, PC online, health, and yesterday's Claude use.
  - It's sent to the phone at `brief.at`, 07:00, as one message.
  - Ari speaks it on "good morning" (`say_brief`).
  - `POST /brief` triggers it.
- **The island** has weather (`GET /island/weather`) and a calendar page that is a month grid, with no events. Argus has **no calendar events source**: no Google Calendar plugin.
- **`ari_listen.idle_seconds()`** reads Windows `GetLastInputInfo`.

## Decided (Claude's calls)

### Core
- **`GET /brief/today`** (guarded) returns `brief_parts` as JSON, using the same sources the phone brief uses. It adds the structured weather from `/island/weather` and the Life Hub tasks due today, if the lifehub plugin exposes them (best effort: skipped if absent). It builds the parts once and doesn't re-word them.
- **"Today's calendar"** is today's Argus schedules plus the Life Hub tasks due today. A Google Calendar source is a follow-up that needs Sas to set up OAuth. The card copes when it's missing.

### When it shows
The island shows it when Sas sits down:
- The PC has had no input for at least 3 hours, then input arrives. This is checked every 5 s with `idle_seconds()`, so it costs nothing.
- The local time is 05:00 to 12:00.
- It hasn't shown today yet, recorded in `data/island-brief.json` as `{day}`.
- The PC isn't in game mode, no fullscreen app is in front, and Ari isn't busy. If any of those blocks it, it waits until the next idle moment before noon.
- If the PC was simply turned on in the morning, the first input after the island starts counts too, under the same window and once-a-day rules.

### The card
- It uses the island's opened shape and its existing details-card styles. No new colours.
- **Top:** "Good morning" plus the date, and the weather on the right (icon, temperature, low to high, rain chance when it's 30% or more).
- **Rows, most important first, in the existing row style:**
  - "3 waiting for you ›" opens the Helios inbox. Shown only when something is waiting.
  - "1 job failed overnight · backup.copy ›" opens Helios runs, in the error tone. Shown only when a job failed.
  - "12 jobs done overnight", in the quiet tone.
  - "Today" lists up to 3 items, time plus label: schedules and tasks.
  - "Backup · last night 02:00 ✓" or a warning.
- **Buttons:** "Read it to me" (Ari speaks `say_brief`, using the existing route) and "Done".
- **Dismissal:**
  - Done, Esc, or a click outside closes it.
  - It closes itself after 25 s if the pointer never entered it.
  - Hovering pauses the timer.
- **Afterwards,** the same card is available until noon from the island's opened Ari page, as one row: "Morning brief ›".

### Not doing
- No sound and no speech unless asked.
- No phone change: the phone already gets the brief at 07:00.

## Tests
- Pure helpers:
  - the sit-down decision: idle history, window, once a day, blocked states;
  - card rows from `brief_parts`, covering the empty, failed and waiting cases;
  - `GET /brief/today`: shape, auth, and the missing-Life-Hub case.
- Offscreen previews: `--brief normal|quiet|trouble` written to `docs/previews/2026-11-19-morning-brief/`.

## Shipping
One PR, held for Sas's preview approval. It's built after the live-activities island work, since both touch `ari_popup.py`.

# Revision after Sas's review (2026-11-19) - replaces the card contents above
Sas wants practical content, not statistics. Approved:
- **Weather.** Open-Meteo `hourly=precipitation_probability,precipitation,weather_code` for today, as well as the existing daily fields.
  - One headline gives when rain is most likely, how likely, and whether it's heavy or thundery. Examples: "Rain likely 2–5 pm (80%), dry morning", "Showers on and off after noon (50%)", "Dry all day". These words are the builder's to refine.
  - Below the headline, a small strip of hourly rain-chance bars from 6 am to 11 pm, drawn in the island's existing tones.
- **From your diary.** A new `diary` workflow, `reminders`, runs daily at 04:00 and on demand.
  - It reads the last 14 days of Life Hub diary entries (`GET /api/diary/<date>`) and extracts what falls on today: appointments, things Sas said they would do, birthdays. Output: `[{text, time|null, mentioned_on}]`.
  - **Local models only, never Claude.** The diary is private.
  - The result is cached, so the brief reads it instantly. If it's missing, the section doesn't appear.
- **Actionable rows only.** "N waiting for you ›" when approvals are pending, and failures that need Sas: a failed backup, or "N things failed overnight ›". Each row is hidden when it's zero.
- **Removed:** jobs-done counts, today's schedules, "backup OK", Claude use.
- **The phone's 07:00 message (`compose_brief`) and Ari's spoken brief (`say_brief`)** get the same content: weather headline, diary reminders, actionable rows.
