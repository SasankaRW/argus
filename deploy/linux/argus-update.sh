#!/usr/bin/env bash
# Update Argus on the laptop to the newest main: back up the database first, then pull, install, check, restart.
# If any step fails, or the new version fails its health check, go back to the previous commit.
# Run by the argus-update timer every 5 minutes (deploy/linux/auto-update.sh sets it up), or by hand:
#   sudo -H -u argus bash /opt/argus/deploy/linux/argus-update.sh
set -euo pipefail
cd /opt/argus
url=http://127.0.0.1:8600
git fetch --quiet origin main
before=$(git rev-parse HEAD)
after=$(git rev-parse origin/main)
[ "$before" = "$after" ] && exit 0   # nothing merged since the last look: quiet
echo "updating ${before:0:8} -> ${after:0:8}: $(git log -1 --format=%s "$after")"

rollback() {
  echo "update failed: rolling back to ${before:0:8}"
  git reset --quiet --hard "$before"
  .venv/bin/pip install --quiet -e ".[plugins,console]" || true
  sudo /usr/bin/systemctl restart argusd argus-worker
  exit 1
}

echo "backing up the database ..."
curl -fsS -X POST -H "Authorization: Bearer ${ARGUS_WORKER_TOKEN:-$(grep -E '^ARGUS_WORKER_TOKEN=' .env | cut -d= -f2-)}" "$url/backups" >/dev/null || echo "(backup failed or argusd down; going on)"
git checkout --quiet main
git reset --quiet --hard "$after"
trap rollback ERR
.venv/bin/pip install --quiet -e ".[plugins,console]"
.venv/bin/python -m argus --check
sudo /usr/bin/systemctl restart argusd argus-worker
for i in $(seq 1 30); do curl -fsS "$url/health" >/dev/null 2>&1 && { echo "updated: ${before:0:8} -> ${after:0:8}"; exit 0; }; sleep 1; done
echo "health check failed"
rollback
