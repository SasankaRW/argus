# Argus code status

Made by `scripts/status.py` from commit `unknown`. Don't edit by hand: run `python scripts/status.py --write` (the release tool does it for every release).

| | |
| --- | --- |
| Version | 0.7.0 |
| Database schema | v18 |
| Plugins | 28 |
| Tests | not counted |
| API routes | 161 |
| Python lines | 55,353 (core 29,026, plugins 9,642, tests 15,820, scripts 865) |
| Modules directly in core/argus | 61 |
| Helios lines | 6,338 (0 test files) |

## Files over 900 lines

| Lines | File |
| ---: | --- |
| 2,614 | `core/argus/api/app.py` |
| 1,891 | `core/argus/ari_popup.py` |
| 1,571 | `plugins/fixer/plugin.py` |
| 1,497 | `core/argus/worker/think.py` |
| 1,483 | `core/argus/console.py` |
| 1,199 | `core/argus/voice_live.py` |
| 952 | `plugins/vault/plugin.py` |
| 922 | `plugins/workstation/plugin.py` |

## Plugins

| Plugin | Name | Runs on | Workflows |
| --- | --- | --- | ---: |
| `advisor` | Money advisor | any | 6 |
| `devhelp` | Dev helper | desktop | 2 |
| `diary` | Diary | desktop | 3 |
| `downloads-organizer` | Downloads organizer | desktop | 4 |
| `duplicate-finder` | Duplicate finder | desktop | 1 |
| `everything` | Everything search | desktop | 1 |
| `eyes` | Eyes clip maker | desktop | 3 |
| `file-tools` | File tools | desktop | 4 |
| `fixer` | Ticket fixer | desktop | 8 |
| `gmail` | Gmail | desktop | 2 |
| `homelab` | Home lab | desktop | 2 |
| `knowledge` | My files (for Ari) | desktop | 3 |
| `lifehub` | Life Hub | any | 4 |
| `notes` | Notes | desktop | 3 |
| `pc-apps` | PC apps | desktop | 5 |
| `pc-keys` | PC keyboard | desktop | 2 |
| `pc-media` | PC media | desktop | 2 |
| `pc-system` | PC status | desktop | 3 |
| `pc-windows` | PC windows | desktop | 4 |
| `phone` | Phone | phone | 16 |
| `routines` | Routines | desktop | 4 |
| `screen` | Screen and clipboard | desktop | 2 |
| `screenshot-renamer` | Screenshot renamer | desktop | 3 |
| `tracker` | Tracker | any | 10 |
| `vault` | Notes vault | desktop | 5 |
| `watchers` | Watchers | any | 4 |
| `web` | Web for Ari | any | 4 |
| `workstation` | Ari's Workstation | any | 10 |
