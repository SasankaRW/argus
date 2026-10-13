#!/usr/bin/env bash
# The notes vault on the laptop (the always-on main copy): Syncthing in Docker + ~/Vault + the Syncthing folder.
#   deploy/linux/vault-setup.sh                         start Syncthing, make ~/Vault, share it; prints the laptop's ID
#   deploy/linux/vault-setup.sh add <ID> <name> <ip>    another device (the PC, the phone) at its Tailscale address
#   deploy/linux/vault-setup.sh status
# Syncthing's own files stay in /opt/syncthing; the vault is ~/Vault (mounted as /var/syncthing/Vault).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
DATA="${SYNCTHING_DIR:-/opt/syncthing}"
VAULT="${VAULT:-$HOME/Vault}"
HELPER=(python3 "$ROOT/scripts/vault_sync.py" --config "$DATA/config/config.xml")

case "${1:-setup}" in
  setup)
    command -v docker >/dev/null || { echo "Docker is needed (it runs Life Hub and Tracker too)"; exit 1; }
    sudo mkdir -p "$DATA" && sudo chown "$(id -u):$(id -g)" "$DATA"
    mkdir -p "$VAULT"
    cat > "$DATA/compose.yaml" <<YAML
services:
  syncthing:
    image: syncthing/syncthing:latest
    container_name: syncthing
    hostname: $(hostname)
    network_mode: host            # listens on 22000 (sync) and 127.0.0.1:8384 (settings)
    environment:
      - PUID=$(id -u)
      - PGID=$(id -g)
      - STGUIADDRESS=127.0.0.1:8384
    volumes:
      - $DATA:/var/syncthing
      - $VAULT:/var/syncthing/Vault
    restart: unless-stopped
YAML
    docker compose -f "$DATA/compose.yaml" up -d
    for _ in $(seq 30); do
      [ -f "$DATA/config/config.xml" ] && curl -fs http://127.0.0.1:8384/rest/noauth/health >/dev/null && break
      sleep 1
    done
    "${HELPER[@]}" setup --vault "$VAULT" --folder-path /var/syncthing/Vault
    echo
    echo "Next: add the PC (and later the phone) here:"
    echo "  deploy/linux/vault-setup.sh add <its ID> pc <its 100.x.y.z>"
    ;;
  add)
    [ $# -eq 4 ] || { echo "usage: vault-setup.sh add <device ID> <name> <tailscale ip>"; exit 1; }
    "${HELPER[@]}" add --id "$2" --name "$3" --address "$4"
    ;;
  status) "${HELPER[@]}" status ;;
  *) echo "unknown command $1 (setup, add, status)"; exit 1 ;;
esac
