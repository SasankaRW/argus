# Moving Argus to the laptop

After this, the laptop runs Argus all the time (argusd, Helios, the schedule, backups) and the PC is only the GPU
worker: the laptop wakes it when there is model or desktop work and shuts it down again when it has been idle.

## What you need

- The laptop with Ubuntu Server 24.04, on a wired connection, BIOS set to "power on after AC loss".
- Tailscale on the laptop (`curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up`).
- The PC: Wake-on-LAN on in the BIOS; network card > Power Management: "Allow this device to wake the computer" and
  "Only allow a magic packet"; Windows Fast Startup off (Control Panel > Power Options > Choose what the power
  buttons do).

## 1. Laptop

```bash
git clone https://github.com/SasankaRW/argus.git ~/argus-src      # private repo: gh auth login first
bash ~/argus-src/deploy/linux/install.sh
```

The script installs Python, creates an `argus` user, puts Argus in `/opt/argus`, makes `argus.yaml` from
`deploy/linux/argus.laptop.yaml` and a `.env` with a new worker token, starts two services at boot (`argusd`,
`argus-worker`), allows only Tailscale and SSH through the firewall, turns on automatic security updates and runs
`tailscale serve` for the phone. Running it again is safe.

Then edit `/opt/argus/argus.yaml`:

| Setting | What |
| --- | --- |
| `power.pc_mac` | the PC's wired card: `ipconfig /all` on the PC, "Physical Address" |
| `approvals.public_url` | the laptop's Tailscale https address |
| `backup.copy_to` | a folder on the PC for the nightly backup copy |

`sudo systemctl restart argusd`, and open Helios at the laptop's Tailscale address.

## 2. Move your data from the PC (once)

On the PC: `.\scripts\dev.ps1 down`. Copy `G:\Projects\argus\data\argus.db` to the laptop
(`scp ... laptop:/tmp/argus.db`), then on the laptop:

```bash
sudo systemctl stop argusd
sudo install -o argus -g argus -m 600 /tmp/argus.db /opt/argus/data/argus.db
sudo systemctl start argusd
```

Your jobs, schedules, rules, approvals and history come along.

## 3. The PC becomes the worker

In `G:\Projects\argus\.env` set `ARGUS_WORKER_TOKEN` to the laptop's (in `/opt/argus/.env`). Then, in PowerShell
as Administrator:

```powershell
.\scripts\pc-worker.ps1 install http://<laptop's Tailscale name>:8600
```

The worker and Ollama now start when the PC boots, before anyone logs in. `.\scripts\pc-worker.ps1 status` shows
them; `remove` undoes it. Don't run `dev.ps1 up` on the PC any more (that starts a second Argus).

## 4. Check

- Helios > Power: the PC shows **On**; press **Shut down**, then **Wake**: it comes back within a minute or two.
- Run "Sort Downloads now": the job runs on the PC while Helios runs on the laptop.
- Leave the PC idle: after 20 minutes the phone says "PC shuts down at …" (tap it to keep it on), and 5 minutes later
  it shuts down. It only does this to a PC Argus woke itself, never while you use the keyboard or mouse.
- Next morning: Helios > Logs shows `backup made` at 02:30, and the copy is in `backup.copy_to` on the PC.

## Updating

The repo is private, so give the laptop's `argus` user read access once: `sudo -u argus ssh-keygen -t ed25519`,
add `/home/argus/.ssh/id_ed25519.pub` as a read-only deploy key on GitHub, and
`sudo -u argus git -C /opt/argus remote set-url origin git@github.com:SasankaRW/argus.git`.

On the laptop: `sudo -u argus /opt/argus/deploy/linux/argus-update.sh` backs up, pulls main, installs, checks and
restarts, and goes back to the previous version if the new one doesn't come up. On the PC the supervisor restarts
the worker by itself after `git pull`.
