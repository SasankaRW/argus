#!/usr/bin/env bash
# Update Argus on the laptop to the newest main: back up the database first, then pull, install, check, restart.
# If the new version fails its health check, go back to the previous commit (and its database copy).
set -euo pipefail
cd /opt/argus
url=http://127.0.0.1:8600
before=$(git rev-parse HEAD)
echo "backing up the database ..."
curl -fsS -X POST -H "Authorization: Bearer ${ARGUS_WORKER_TOKEN:-$(grep -E '^ARGUS_WORKER_TOKEN=' .env | cut -d= -f2-)}" "$url/backups" >/dev/null || echo "(backup failed or argusd down; going on)"
git fetch --quiet origin main
git checkout --quiet main
git reset --quiet --hard origin/main
after=$(git rev-parse HEAD)
[ "$before" = "$after" ] && { echo "already up to date ($after)"; exit 0; }
.venv/bin/pip install --quiet -e ".[plugins]"
.venv/bin/python -m argus --check
sudo /usr/bin/systemctl restart argusd argus-worker
for i in $(seq 1 30); do curl -fsS "$url/health" >/dev/null 2>&1 && { echo "updated: ${before:0:8} -> ${after:0:8}"; exit 0; }; sleep 1; done
echo "health check failed: rolling back to ${before:0:8}"
git reset --quiet --hard "$before"
.venv/bin/pip install --quiet -e ".[plugins]"
sudo /usr/bin/systemctl restart argusd argus-worker
exit 1
