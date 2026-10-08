#!/usr/bin/env bash
# Install Argus on the laptop (Ubuntu Server 24.04). Run once, as your normal user with sudo:
#   curl -fsSL https://raw.githubusercontent.com/... | bash      (private repo: clone first, then run this file)
#   bash deploy/linux/install.sh
# Safe to run again: it only adds what is missing.
set -euo pipefail
REPO=${ARGUS_REPO:-https://github.com/SasankaRW/argus.git}
DIR=/opt/argus

say() { printf '\n\033[33m== %s\033[0m\n' "$*"; }

say "packages"
sudo apt-get update -qq
sudo apt-get install -y -qq git python3 python3-venv python3-pip sqlite3 curl ufw unattended-upgrades >/dev/null

say "user and folder"
id argus >/dev/null 2>&1 || sudo useradd --system --create-home --home-dir /home/argus --shell /usr/sbin/nologin argus
if [ ! -d "$DIR/.git" ]; then
  sudo git clone "$REPO" "$DIR"
fi
sudo mkdir -p "$DIR/data" "$DIR/logs"
sudo chown -R argus:argus "$DIR"

say "python environment"
sudo -u argus python3 -m venv "$DIR/.venv"
sudo -u argus "$DIR/.venv/bin/pip" install --quiet --upgrade pip
sudo -u argus "$DIR/.venv/bin/pip" install --quiet -e "$DIR[plugins,console]"

say "settings"
if [ ! -f "$DIR/argus.yaml" ]; then
  sudo -u argus cp "$DIR/deploy/linux/argus.laptop.yaml" "$DIR/argus.yaml"
  echo "created $DIR/argus.yaml from deploy/linux/argus.laptop.yaml: fill in power.pc_mac and approvals"
fi
if [ ! -f "$DIR/.env" ]; then
  tok=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
  sudo -u argus sh -c "sed 's/^ARGUS_WORKER_TOKEN=.*/ARGUS_WORKER_TOKEN=$tok/' '$DIR/.env.example' > '$DIR/.env'"
  echo "created $DIR/.env with a new worker token (copy it to the PC's .env)"
fi
sudo chmod 600 "$DIR/.env"
sudo -u argus "$DIR/.venv/bin/python" -m argus --config "$DIR/argus.yaml" --check

say "services (start at boot, restart on failure)"
sudo cp "$DIR/deploy/linux/argusd.service" "$DIR/deploy/linux/argus-worker.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now argusd argus-worker
echo "argus ALL=(root) NOPASSWD: /usr/bin/systemctl restart argusd argus-worker" | sudo tee /etc/sudoers.d/argus-update >/dev/null

say "firewall: only Tailscale and SSH"
sudo ufw default deny incoming >/dev/null
sudo ufw default allow outgoing >/dev/null
sudo ufw allow in on tailscale0 >/dev/null
sudo ufw allow OpenSSH >/dev/null
sudo ufw --force enable >/dev/null

say "automatic security updates"
sudo dpkg-reconfigure -f noninteractive unattended-upgrades >/dev/null

say "Tailscale: Helios for the phone"
if command -v tailscale >/dev/null; then
  sudo tailscale serve --bg 8600 || echo "run: sudo tailscale serve --bg 8600"
else
  echo "install Tailscale first: curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up"
fi

for i in $(seq 1 20); do curl -fsS http://127.0.0.1:8600/health >/dev/null 2>&1 && break; sleep 1; done
curl -fsS http://127.0.0.1:8600/health >/dev/null && say "Argus is running: http://127.0.0.1:8600 (and your Tailscale https address)" \
  || { echo "argusd did not come up: journalctl -u argusd -n 50"; exit 1; }
