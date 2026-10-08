# Moving Argus to the laptop

After this, the laptop runs Argus all the time (argusd, Helios, the schedule, backups) and the PC is only the GPU
worker: the laptop wakes it when there is model or desktop work and shuts it down again when it has been idle.

## What you need

- The laptop with Arch Linux (any install that boots to a shell, with a network connection), wired if you can,
  BIOS set to "power on after AC loss". The scripts are for Arch (`deploy/linux/install-arch.sh`); Ubuntu Server
  24.04 uses `install.sh` and the same steps otherwise.
- The PC: Wake-on-LAN on in the BIOS; network card > Power Management: "Allow this device to wake the computer" and
  "Only allow a magic packet"; Windows Fast Startup off (Control Panel > Power Options > Choose what the power
  buttons do).
- Tailscale on the phone and PC is already there; the laptop gets it in step 1.

## 0. Laptop basics (nothing installed yet)

On the laptop, at its keyboard (Hyprland: open a terminal):

```bash
sudo pacman -Syu                         # you update by hand: do it now, reboot if the kernel changed
sudo pacman -S --needed git openssh github-cli
sudo systemctl enable --now sshd
gh auth login && gh auth setup-git       # the repo is private: this lets you clone it
ip -br a                                 # the laptop's LAN address, to reach it from the PC the first time
```

From the PC (PowerShell; Windows has `ssh` and `scp`): `ssh <your-user>@<laptop LAN address>`. Make a key if you
have none (`ssh-keygen -t ed25519`) and `type $HOME\.ssh\id_ed25519.pub | ssh <you>@<laptop> "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys"`.
After step 1 use the laptop's Tailscale name instead of the LAN address.

## 1. Argus on the laptop

```bash
git clone https://github.com/SasankaRW/argus.git ~/argus-src
bash ~/argus-src/deploy/linux/install-arch.sh
```

The script installs the packages (Python, Docker, Tailscale, firewall), signs Tailscale in (open the link it prints),
keeps the laptop running with the lid closed (from the next boot), creates an `argus` user, copies Argus into
`/opt/argus` (with Python 3.12 from uv if Arch's own Python is newer than 3.13), makes `argus.yaml` from
`deploy/linux/argus.laptop.yaml` and a `.env` with a new worker token, starts two services at boot (`argusd`,
`argus-worker`), allows only Tailscale and SSH through the firewall and runs `tailscale serve` for the phone. It
never updates the system: that stays your `pacman -Syu`. Running it again is safe. Log out and in once afterwards
(Docker group).

In the Tailscale admin console (DNS page) HTTPS certificates must be on, or `tailscale serve` has nothing to serve.

Then edit `/opt/argus/argus.yaml` (`sudo -H -u argus nano /opt/argus/argus.yaml`):

| Setting | What |
| --- | --- |
| `power.pc_mac` | the PC's wired card: `ipconfig /all` on the PC, "Physical Address" |
| `approvals.public_url` | the laptop's Tailscale https address (`tailscale status` shows the name) |
| `backup.copy_to` | a folder on the PC for the nightly backup copy |

and `/opt/argus/.env` (`sudo -H -u argus nano /opt/argus/.env`): copy `ARGUS_ADMIN_PASSWORD`, `TRACKER_API_KEY` and
`LIFEHUB_API_KEY` from the PC's `G:\Projects\argus\.env`; leave the new `ARGUS_WORKER_TOKEN`.
`sudo systemctl restart argusd`, and open Helios at the laptop's Tailscale address.

## 2. Move your data from the PC (once)

On the PC: `.\scripts\dev.ps1 down`. Copy `G:\Projects\argus\data\argus.db` to the laptop
(`scp G:\Projects\argus\data\argus.db <you>@<laptop>:/tmp/argus.db`), then on the laptop:

```bash
sudo systemctl stop argusd
sudo install -o argus -g argus -m 600 /tmp/argus.db /opt/argus/data/argus.db
sudo systemctl start argusd
```

Your jobs, schedules, rules, approvals and history come along.

### Tracker

Tracker has no Git remote, so copy the folder, and its database and attachments out of the PC's Docker:

```powershell
cd G:\Projects\tracker
docker compose exec -T db pg_dump -U tracker -Fc tracker -f /tmp/tracker.dump
docker compose cp db:/tmp/tracker.dump .\tracker.dump
docker compose cp app:/data/uploads .\uploads-export
cd ..
tar --exclude=node_modules --exclude=dist --exclude=_to_delete -czf tracker.tgz tracker
scp tracker.tgz tracker\tracker.dump <you>@<laptop>:~/
scp -r tracker\uploads-export <you>@<laptop>:~/
```

On the laptop:

```bash
tar xzf ~/tracker.tgz -C ~
bash ~/argus-src/deploy/linux/tracker-arch.sh restore ~/tracker.dump ~/uploads-export
```

Tracker now answers on the laptop at `http://127.0.0.1:8282` and, through Tailscale, at
`https://<laptop>.<tailnet>.ts.net:8443` (the phone and PC use that one). Check your tickets and attachments there,
then on the PC stop the old one (`docker compose down` in `G:\Projects\tracker`; keep the volumes a few weeks as a
backup). Updating Tracker later: copy the new folder over and `bash ~/argus-src/deploy/linux/tracker-arch.sh up`
(its web build is baked into the Docker image, so it needs the rebuild). The `tracker` plugin runs on the laptop, so
its Tracker address stays `http://127.0.0.1:8282`; the fixer runs on the PC, so set **its** `tracker_url` to the
https address above (Helios > plugins > fixer > Settings).

## 3. The PC becomes the worker

In `G:\Projects\argus\.env` set `ARGUS_WORKER_TOKEN` to the laptop's (in `/opt/argus/.env`). Check first (it only
looks): the laptop answers and takes the token, Argus on the PC is stopped, Ollama has the models, Fast Startup is
off, and the wired card's address for `power.pc_mac`:

```powershell
.\scripts\pc-worker.ps1 check https://<laptop's Tailscale name>
```

Fix any `[!!]` line, then, in PowerShell as Administrator:

```powershell
.\scripts\pc-worker.ps1 install https://<laptop's Tailscale name>
```

The worker and Ollama now start when the PC boots, before anyone logs in. When you log in, a second supervisor
starts what needs your session: Ari's PC tools (open apps, volume, ...), "Hey Ari" and the island, all talking to
the laptop (`logs/supervisor-desk.log`). `.\scripts\pc-worker.ps1 status` shows
them; `remove` undoes it. Don't run `dev.ps1 up` on the PC any more (that starts a second Argus).

## 4. Check

- Log in on the PC: the island is at the top of the screen and "Hey Ari" answers; Helios > Map shows the PC
  online (its GPU worker and `desktop-<pc>` are one PC there).
- Helios > Power: the PC shows **On**; press **Shut down**, then **Wake**: it comes back within a minute or two.
- Open Tracker from the phone (`https://<laptop>...:8443`); label a small ticket `ai-fix`: the fixer on the PC plans it
  and the plan shows up in Tracker.
- Run "Sort Downloads now": the job runs on the PC while Helios runs on the laptop.
- Leave the PC idle: after 20 minutes the phone says "PC shuts down at …" (tap it to keep it on), and 5 minutes later
  it shuts down. It only does this to a PC Argus woke itself, never while you use the keyboard or mouse.
- Next morning: Helios > Logs shows `backup made` at 02:30, and the copy is in `backup.copy_to` on the PC.

## What runs where

- **The laptop:** Argus itself (Helios, Ari's chat and memory, schedules, backups), plugins that only call web
  services (`runs_on: any`), and Piper for Ari's voice in Helios and on the phone.
- **The PC's worker:** everything that needs a model. `models.needs: [gpu]` in the laptop's `argus.yaml` sends
  Ari's replies, Ask and guidance there. While the PC is off, Ari says "Waking the PC, about a minute" and the
  laptop wakes it. Plugins for your files and apps run there too (downloads organizer, screenshots, ...).
- **The PC, logged in:** "Hey Ari", the island and the PC tools. The listener makes Ari's voice on the PC itself
  (the expressive voice on its GPU, else Piper there), so speech never goes round through the laptop.
- **Ari's expressive voice in Helios and on the phone** comes from the PC too: `ari.expressive_share: true` in the
  PC's `argus.yaml` (the voice answers over Tailscale, with the worker token; `pc-worker.ps1 install` opens port
  8611 to Tailscale devices only), and on the laptop `ari.voice_engine: expressive` with
  `expressive_url: http://<pc>.<tailnet>.ts.net:8611`. While the PC is off, Helios gets Piper from the laptop.
- **A plugin whose address is `127.0.0.1`** runs where that service is: `plugins.runs_on: {web: desktop}` for
  SearXNG on the PC, `{tracker: laptop}` once Tracker runs on the laptop. `/opt/argus/.venv/bin/python -m argus
  --check` lists the ones that need it.
- **Ollama** must answer on the PC: `.\scripts\pc-worker.ps1 status` says whether it does.

## Updating

**By itself (once):** `bash /opt/argus/deploy/linux/auto-update.sh` on the laptop. It gives the `argus` user a
read-only deploy key (the repo is private; with `gh` signed in the key is added for you, otherwise it prints the key
and the GitHub page to paste it on) and starts a timer. Every 5 minutes it runs `argus-update.sh`, which does nothing
unless main moved. Otherwise it backs up the database, pulls main, installs, checks and restarts, and goes back to the
previous version if the new one doesn't come up. So after `.\scripts\dev.ps1 merge` (or `ship`) the laptop is on
the new code within about 5 minutes. Each run: `journalctl -u argus-update -n 30`. Off:
`bash /opt/argus/deploy/linux/auto-update.sh off`.

**By hand:** `sudo -H -u argus bash /opt/argus/deploy/linux/argus-update.sh` does one update the same way.

On the PC the supervisor restarts the worker by itself after `git pull` (`ship` pulls main at the end); Ari's
listener and the island: `.\scripts\pc-worker.ps1 restart`.
