#!/usr/bin/env bash
# One time, on the laptop: let Argus update itself after every merge.
#   bash /opt/argus/deploy/linux/auto-update.sh          set up (deploy key, timer)
#   bash /opt/argus/deploy/linux/auto-update.sh off      stop updating by itself
#
# The repo is private, so the argus user gets its own read-only deploy key. With GitHub's CLI signed in (gh auth
# login) the key is added for you; otherwise the script prints it and where to paste it. Then a timer runs
# argus-update.sh every 5 minutes: it does nothing unless main moved, and goes back to the previous version if the
# new one doesn't come up (journalctl -u argus-update shows each run).
set -euo pipefail
DIR=/opt/argus
REPO_SSH=${ARGUS_REPO_SSH:-git@github.com:SasankaRW/argus.git}
REPO_NAME=${ARGUS_REPO_NAME:-SasankaRW/argus}
say() { printf '\n== %s\n' "$*"; }

if [ "${1:-}" = "off" ]; then
  sudo systemctl disable --now argus-update.timer
  echo "Argus no longer updates by itself (sudo -H -u argus $DIR/deploy/linux/argus-update.sh still does it once)."
  exit 0
fi

say "the argus user's deploy key"
KEY=/home/argus/.ssh/id_ed25519
if ! sudo test -f "$KEY"; then
  sudo -H -u argus mkdir -p -m 700 /home/argus/.ssh
  sudo -H -u argus ssh-keygen -q -t ed25519 -N "" -C "argus@$(hostname) (read-only deploy key)" -f "$KEY"
fi
sudo -H -u argus sh -c 'ssh-keyscan -t ed25519 github.com 2>/dev/null >> ~/.ssh/known_hosts; sort -u -o ~/.ssh/known_hosts ~/.ssh/known_hosts'
sudo -H -u argus git -C "$DIR" remote set-url origin "$REPO_SSH"

if ! sudo -H -u argus git -C "$DIR" ls-remote --quiet origin HEAD >/dev/null 2>&1; then
  PUB=$(sudo cat "$KEY.pub")
  if command -v gh >/dev/null && gh auth status >/dev/null 2>&1; then
    echo "$PUB" | gh repo deploy-key add - --repo "$REPO_NAME" --title "argus on $(hostname)" \
      && echo "deploy key added to $REPO_NAME (read-only)"
  else
    echo "Add this as a read-only deploy key: https://github.com/$REPO_NAME/settings/keys/new"
    echo
    echo "$PUB"
    echo
    read -r -p "Press Enter once it is added ... " _
  fi
  sudo -H -u argus git -C "$DIR" ls-remote --quiet origin HEAD >/dev/null \
    || { echo "GitHub still refuses the key: check it was added to $REPO_NAME, then run this again"; exit 1; }
fi
echo "the argus user can read the repo"

say "the update timer"
sudo cp "$DIR/deploy/linux/argus-update.service" "$DIR/deploy/linux/argus-update.timer" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now argus-update.timer
systemctl list-timers argus-update.timer --no-pager | head -3
echo
echo "Done: after you merge, the laptop is on the new main within about 5 minutes."
echo "Each run: journalctl -u argus-update -n 30    Off: bash $DIR/deploy/linux/auto-update.sh off"
