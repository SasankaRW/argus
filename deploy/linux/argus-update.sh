#!/usr/bin/env bash
# Update Argus on the laptop to the newest main: back up the database first, then pull, install, check, restart.
# If any step fails, or the new version fails its health check, go back to the previous commit.
# Run by the argus-update timer every 5 minutes (deploy/linux/auto-update.sh sets it up), or by hand:
#   sudo -H -u argus bash /opt/argus/deploy/linux/argus-update.sh
# Every run leaves two small files in argusd's data dir for GET /deploy (Helios, dev.ps1 deployed):
#   last-check.json   {checked_at, origin, behind, error?}                 after each fetch
#   last-update.json  {at, from, to, subject, result, step, detail}        after each attempt
# Every step that can wait on the network or a busy process has its own time limit, together well under the unit's
# TimeoutStartSec=15min: a run that systemd kills half way would leave new code checked out but never installed or
# restarted (and the next run would see "nothing new"). If systemd stops it anyway, the TERM trap rolls back.
set -euo pipefail
cd /opt/argus
url=http://127.0.0.1:8600
data=data   # argusd's database is data/argus.db (database.path in argus.yaml), so its data dir is this one
mkdir -p "$data"
exec 9>"$data/.update.lock"
flock -n 9 || exit 0   # another run (or a hand-run) is going: quiet

step=fetch
backup_note=""
before=""
after=""
subject=""

# JSON text: escape backslash and quote, drop control characters
jesc() { printf '%s' "$1" | tr -d '\000-\037' | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'; }
# write_json <file name> <json>: a temp file in data/, then mv, so argusd never reads half a file
write_json() {
  local tmp
  tmp=$(mktemp "$data/.tmp.XXXXXX")
  printf '%s\n' "$2" > "$tmp"
  chmod 644 "$tmp"
  mv -f "$tmp" "$data/$1"
}
write_check() {  # write_check <error text, may be empty>
  local behind origin err=""
  behind=$(git rev-list --count HEAD..origin/main 2>/dev/null || echo 0)
  origin=$(git rev-parse origin/main 2>/dev/null || echo "")
  if [ -n "$1" ]; then err=",\"error\":\"$(jesc "$1")\""; fi
  write_json last-check.json "{\"checked_at\":$(date +%s),\"origin\":\"$origin\",\"behind\":$behind$err}"
}
write_update() {  # write_update <result> <detail>
  write_json last-update.json "{\"at\":$(date +%s),\"from\":\"$before\",\"to\":\"$after\",\"subject\":\"$(jesc "$subject")\",\"result\":\"$1\",\"step\":\"$step\",\"detail\":\"$(jesc "$2")\"}"
}

fail_early() {  # before anything was changed: just record that the check failed
  local rc=$?
  trap - ERR
  write_check "$step failed (exit $rc)" || true
  exit "$rc"
}
trap fail_early ERR
on_term() {  # systemd stops a run that went over its time: record it (and undo the reset, if it happened)
  if [ "$reset_done" = 1 ]; then rollback "stopped during $step (over its time)"; fi
  trap - ERR
  write_check "stopped during $step (over its time)" || true
  exit 143
}
reset_done=0
trap on_term TERM

if ! timeout 120 git fetch --quiet origin main; then
  write_check "git fetch failed"
  exit 1
fi
write_check ""
step=compare
before=$(git rev-parse HEAD)
after=$(git rev-parse origin/main)
[ "$before" = "$after" ] && exit 0   # nothing merged since the last look: quiet
subject=$(git log -1 --format=%s "$after")
echo "updating ${before:0:8} -> ${after:0:8}: $subject"

# the package versions CI tested (constraints.txt), when this commit has them
pins() { [ -f constraints.txt ] && echo "-c constraints.txt" || true; }

rollback() {  # rollback [detail]: the note is written FIRST, so it is there even if the rest goes wrong
  local rc=$?
  trap - ERR
  local detail="${1:-$step failed (exit $rc)}"
  if [ -n "$backup_note" ]; then detail="$detail; $backup_note"; fi
  write_update rolled_back "$detail" || true
  echo "update failed ($step): rolling back to ${before:0:8}"
  if ! git reset --quiet --hard "$before"; then
    step=rollback
    write_update failed "$detail; the rollback itself failed (git reset)" || true
    exit 1
  fi
  # shellcheck disable=SC2046
  timeout 240 .venv/bin/pip install --quiet -e ".[plugins,console]" $(pins) || true
  sudo /usr/bin/systemctl restart argusd argus-worker || true
  write_check "" || true
  exit 1
}

step=backup
echo "backing up the database ..."
curl -fsS --max-time 180 -X POST -H "Authorization: Bearer ${ARGUS_WORKER_TOKEN:-$(grep -E '^ARGUS_WORKER_TOKEN=' .env | cut -d= -f2-)}" "$url/backups" >/dev/null || { backup_note="backup failed or argusd down"; echo "($backup_note; going on)"; }
step=reset
git checkout --quiet main
git reset --quiet --hard "$after"
reset_done=1
trap rollback ERR
step=pip
# shellcheck disable=SC2046
timeout 360 .venv/bin/pip install --quiet -e ".[plugins,console]" $(pins)
step=check
timeout 120 .venv/bin/python -m argus --check
step=restart
sudo /usr/bin/systemctl restart argusd argus-worker
step=health
for i in $(seq 1 30); do
  if curl -fsS --max-time 3 "$url/health" >/dev/null 2>&1; then
    detail="updated"
    if [ -n "$backup_note" ]; then detail="updated; $backup_note"; fi
    trap - ERR
    write_update updated "$detail" || true   # the update itself worked: a note that can't be written mustn't fail it
    write_check "" || true
    echo "updated: ${before:0:8} -> ${after:0:8}"
    exit 0
  fi
  sleep 1
done
echo "health check failed"
rollback "health check failed after 30 s"
