# Argus code status

Made by `scripts/status.py` from commit `23bb40d`. Don't edit by hand: run `python scripts/status.py --write` (the release tool does it for every release).

| | |
| --- | --- |
| Version | 0.9.0 |
| Database schema | v19 |
| Plugins | 28 |
| Tests | 947 |
| API routes | 162 |
| Python lines | 57,035 (core 29,855, plugins 9,648, tests 16,414, scripts 1,118) |
| Modules directly in core/argus | 45 |
| Helios lines | 7,344 (5 test files) |

## Files over 900 lines

| Lines | File |
| ---: | --- |
| 1,914 | `core/argus/ari/ari_popup.py` |
| 1,571 | `plugins/fixer/plugin.py` |
| 1,517 | `core/argus/console.py` |
| 1,497 | `core/argus/worker/think.py` |
| 1,199 | `core/argus/voice/voice_live.py` |
| 952 | `plugins/vault/plugin.py` |
| 928 | `plugins/workstation/plugin.py` |
| 902 | `core/argus/ari/ari_listen.py` |

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
