# Restoring a backup

Argus makes a backup of its database every night (`backup.at`, 02:30 by default) into `data/backups/` and checks
each one. The PC's worker keeps a copy in `backup.copy_to`. This page is how to put one back.

`argusd --restore` does the work. It never deletes anything:

- it checks the backup first (it opens, passes SQLite's integrity check, has its jobs) and refuses a bad one;
- it refuses a backup made by a newer Argus than the one installed (upgrade first). An older one is fine: argusd
  brings it up to date on its next start;
- it refuses while argusd is running;
- the database it replaces, with its `-wal` and `-shm` files, is moved to `data/replaced/argus-<date>/`.

Without `--yes` it only says what it would do.

## On the laptop

```bash
# 1. Stop Argus, and the updater so it doesn't start Argus again half way
sudo systemctl stop argus-update.timer argusd argus-worker

# 2. See what would happen (the newest backup; or name one: --restore argus-20261112-023000.db)
cd /opt/argus
sudo -H -u argus .venv/bin/python -m argus --restore latest

# 3. Do it
sudo -H -u argus .venv/bin/python -m argus --restore latest --yes

# 4. Check, then start everything again
sudo -H -u argus .venv/bin/python -m argus --check
sudo systemctl start argusd argus-worker argus-update.timer
```

Then open Helios: Runs should show the jobs up to the night of the backup.

### From the PC's copy (the laptop's disk was lost)

After a fresh install (deploy/README.md), copy the newest file from `backup.copy_to` on the PC:

```bash
scp <you>@<pc>:<backup.copy_to>/argus-YYYYMMDD-HHMMSS.db /tmp/
sudo install -o argus -g argus -m 600 /tmp/argus-YYYYMMDD-HHMMSS.db /opt/argus/data/backups/
```

then steps 1 to 4 above, naming that file in place of `latest`.

## On the PC (Argus run by hand)

```powershell
.\scripts\dev.ps1 down
.venv\Scripts\python.exe -m argus --restore latest          # look first
.venv\Scripts\python.exe -m argus --restore latest --yes
.\scripts\dev.ps1 up
```

## Undoing a restore

The database that was replaced is in `data/replaced/argus-<date>/`. Stop argusd, move the current `argus.db` aside
the same way, move the old one (with its `-wal` and `-shm` files, if any) back into `data/`, and start argusd.

## Practice once

A backup is only proven once it has been put back. Do steps 1 to 4 on the laptop once, on a quiet evening, and
write the date here:

| Date | Backup | Jobs after | Notes |
| --- | --- | --- | --- |
| | | | |

Exit codes of `argusd --restore`: 0 done (or the plan shown), 2 bad config, 4 refused (the reason is printed).
