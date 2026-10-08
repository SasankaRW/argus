#!/usr/bin/env bash
# Install Argus on the laptop (Arch Linux). Run once as your normal user (not root) with sudo:
#   bash deploy/linux/install-arch.sh
# Safe to run again: it only adds what is missing.
# It does NOT update the system: run `sudo pacman -Syu` yourself first (and reboot if the kernel changed).
set -euo pipefail
[ "$(id -u)" -ne 0 ] || { echo "run as your normal user, not root (it uses sudo)"; exit 1; }
REPO=${ARGUS_REPO:-https://github.com/SasankaRW/argus.git}
SRC=$(cd "$(dirname "$0")/../.." && pwd)    # the checkout this script came from (works while the repo is private)
DIR=/opt/argus
ME=$(id -un)

say() { printf '\n\033[33m== %s\033[0m\n' "$*"; }

say "packages"
if ! sudo pacman -S --needed --noconfirm git python python-pip sqlite curl nano openssh ufw tailscale \
      docker docker-compose docker-buildx >/dev/null; then
  echo "pacman could not install them. Update the system first: sudo pacman -Syu   (then run this again)"
  exit 1
fi

say "clock, ssh, Tailscale, Docker"
sudo systemctl enable --now systemd-timesyncd sshd tailscaled docker
sudo usermod -aG docker "$ME"          # Tracker's docker compose, without sudo (log out and in once)
if ! sudo tailscale status >/dev/null 2>&1; then
  echo "Tailscale is not signed in: open the link it prints (on your phone is fine), then this goes on"
  sudo tailscale up
fi

say "keep running with the lid closed"
sudo mkdir -p /etc/systemd/logind.conf.d
printf '[Login]\nHandleLidSwitch=ignore\nHandleLidSwitchExternalPower=ignore\nHandleLidSwitchDocked=ignore\n' \
  | sudo tee /etc/systemd/logind.conf.d/argus-lid.conf >/dev/null
# applied at the next boot (restarting logind now would end a desktop session)

say "user and folder"
id argus >/dev/null 2>&1 || sudo useradd --system --create-home --home-dir /home/argus --shell "$(command -v nologin)" argus
if [ ! -d "$DIR/.git" ]; then
  sudo git -c safe.directory="$SRC/.git" clone --quiet "$SRC" "$DIR"
  sudo git -C "$DIR" remote set-url origin "$REPO"
fi
sudo mkdir -p "$DIR/data" "$DIR/logs"
sudo chown -R argus:argus "$DIR"
cd "$DIR"    # uv and pip look for config in the current folder and its parents: not your home, which argus can't read

say "python environment"
# Arch's python is the newest one; Argus is tested on 3.11-3.13. On anything newer, use a pinned 3.12 (via uv).
PYV=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
case "$PYV" in
  3.11|3.12|3.13) sudo -H -u argus python3 -m venv "$DIR/.venv" ;;
  *)
    echo "system python is $PYV: using Python 3.12 from uv instead"
    sudo pacman -S --needed --noconfirm uv >/dev/null
    sudo -H -u argus env UV_PYTHON_INSTALL_DIR="$DIR/pythons" uv venv --python 3.12 --seed "$DIR/.venv"
    ;;
esac
sudo -H -u argus "$DIR/.venv/bin/pip" install --quiet --upgrade pip
sudo -H -u argus "$DIR/.venv/bin/pip" install --quiet -e "$DIR[plugins,console]"

say "settings"
if [ ! -f "$DIR/argus.yaml" ]; then
  sudo -H -u argus cp "$DIR/deploy/linux/argus.laptop.yaml" "$DIR/argus.yaml"
  echo "created $DIR/argus.yaml from deploy/linux/argus.laptop.yaml: fill in power.pc_mac and approvals"
fi
if [ ! -f "$DIR/.env" ]; then
  tok=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
  sudo -H -u argus sh -c "sed 's/^ARGUS_WORKER_TOKEN=.*/ARGUS_WORKER_TOKEN=$tok/' '$DIR/.env.example' > '$DIR/.env'"
  echo "created $DIR/.env with a new worker token (copy it to the PC's .env)"
fi
sudo chmod 600 "$DIR/.env"
sudo -H -u argus "$DIR/.venv/bin/python" -m argus --config "$DIR/argus.yaml" --check

say "services (start at boot, restart on failure)"
sudo cp "$DIR/deploy/linux/argusd.service" "$DIR/deploy/linux/argus-worker.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now argusd argus-worker
echo "argus ALL=(root) NOPASSWD: /usr/bin/systemctl restart argusd argus-worker" | sudo tee /etc/sudoers.d/argus-update >/dev/null
sudo chmod 440 /etc/sudoers.d/argus-update
if [ -f /etc/systemd/system/argus-update.timer ]; then  # set up before: keep its units current
  sudo cp "$DIR/deploy/linux/argus-update.service" "$DIR/deploy/linux/argus-update.timer" /etc/systemd/system/
  sudo systemctl daemon-reload
else
  echo "To update by itself after every merge: bash $DIR/deploy/linux/auto-update.sh (once)"
fi

say "firewall: only Tailscale and SSH"
sudo ufw default deny incoming >/dev/null
sudo ufw default allow outgoing >/dev/null
sudo ufw allow in on tailscale0 >/dev/null
sudo ufw allow 22/tcp >/dev/null
sudo ufw --force enable >/dev/null
sudo systemctl enable ufw >/dev/null

say "Tailscale: Helios for the phone"
sudo tailscale serve --bg 8600 || echo "run: sudo tailscale serve --bg 8600 (turn on HTTPS in the Tailscale admin console > DNS first)"

for i in $(seq 1 20); do curl -fsS http://127.0.0.1:8600/health >/dev/null 2>&1 && break; sleep 1; done
curl -fsS http://127.0.0.1:8600/health >/dev/null && say "Argus is running: http://127.0.0.1:8600 (and your Tailscale https address)" \
  || { echo "argusd did not come up: journalctl -u argusd -n 50"; exit 1; }
echo "Docker group: log out and in (or reboot) once before running Tracker's docker compose."
