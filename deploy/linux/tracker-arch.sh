#!/usr/bin/env bash
# Run Tracker on the laptop (Docker), reachable only through Tailscale.
#   bash deploy/linux/tracker-arch.sh up [folder]                  start (or rebuild) Tracker; default folder ~/tracker
#   bash deploy/linux/tracker-arch.sh restore dump uploads [folder]  load the PC's database and attachments (once)
# The folder is a copy of G:\Projects\tracker, with its .env (the same API_KEY Argus uses as TRACKER_API_KEY).
# Tracker listens on 127.0.0.1:8282 only (Docker would bypass the firewall on a public port), and
# `tailscale serve` publishes it at https://<laptop>.<tailnet>.ts.net:8443.
set -euo pipefail
cmd=${1:-up}
case "$cmd" in
  up) dir=${2:-$HOME/tracker} ;;
  restore) dump=${2:?dump file}; uploads=${3:?uploads folder}; dir=${4:-$HOME/tracker} ;;
  *) echo "usage: $0 up [folder] | restore dump uploads [folder]"; exit 1 ;;
esac
[ -f "$dir/docker-compose.yml" ] || { echo "no Tracker in $dir (copy the folder from the PC first)"; exit 1; }
[ -f "$dir/.env" ] || { echo "no .env in $dir (copy it from the PC: it holds the passwords and API_KEY)"; exit 1; }
docker info >/dev/null 2>&1 || { echo "docker is not usable: log out and in once (docker group) or: sudo systemctl start docker"; exit 1; }
cd "$dir"
if grep -q '^PORT=' .env; then sed -i 's/^PORT=.*/PORT=127.0.0.1:8282/' .env; else echo 'PORT=127.0.0.1:8282' >> .env; fi
chmod 600 .env

if [ "$cmd" = restore ]; then
  [ "$(docker compose ps -q app | wc -l)" -eq 0 ] || { echo "Tracker is already running here: docker compose down -v first (this loads into an empty database)"; exit 1; }
  docker compose up -d db
  for i in $(seq 1 30); do docker compose exec -T db pg_isready -U tracker >/dev/null 2>&1 && break; sleep 1; done
  docker compose cp "$dump" db:/tmp/tracker.dump
  docker compose exec -T db pg_restore -U tracker -d tracker --clean --if-exists --no-owner /tmp/tracker.dump
  docker compose up -d --build
  docker compose cp "$uploads/." app:/data/uploads
  echo "database and attachments loaded"
else
  docker compose up -d --build
fi

for i in $(seq 1 30); do curl -fsS http://127.0.0.1:8282/ >/dev/null 2>&1 && break; sleep 1; done
curl -fsS http://127.0.0.1:8282/ >/dev/null || { echo "Tracker did not answer: docker compose logs app"; exit 1; }
sudo tailscale serve --bg --https=8443 8282 || echo "run: sudo tailscale serve --bg --https=8443 8282"
echo "Tracker is running: http://127.0.0.1:8282 here, https://<laptop>.<tailnet>.ts.net:8443 from the PC and phone"
