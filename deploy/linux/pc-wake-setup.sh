#!/usr/bin/env bash
# The Wake button from a laptop on Wi-Fi. Many home routers don't pass a broadcast from Wi-Fi to their wired ports,
# so the usual Wake-on-LAN packet never reaches the PC. argusd also sends it straight to the PC (power.pc_ip), but
# while the PC is off the laptop forgets which card that address belongs to and drops the packet. This keeps the
# PC's address tied to its card (a permanent neighbour entry), now and every time the laptop's network comes up
# (a NetworkManager dispatcher script).
#
# Set power.pc_ip and power.pc_mac in /opt/argus/argus.yaml first (keep the PC's address fixed in the router), then:
#   sudo bash /opt/argus/deploy/linux/pc-wake-setup.sh
#   off:  sudo bash /opt/argus/deploy/linux/pc-wake-setup.sh off
set -euo pipefail
HOOK=/etc/NetworkManager/dispatcher.d/90-argus-pc-wake
CONF=${ARGUS_CONF:-/opt/argus/argus.yaml}
PY=${ARGUS_PY:-/opt/argus/.venv/bin/python}

[ "$(id -u)" = 0 ] || { echo "Run it as root: sudo bash $0" >&2; exit 1; }

if [ "${1:-}" = "off" ]; then
  if [ -f "$HOOK" ]; then
    ip_=$(sed -n 's/^PC_IP=//p' "$HOOK")
    rm -f "$HOOK"
    [ -n "$ip_" ] && ip neigh del "$ip_" dev "$(ip -4 route get "$ip_" | sed -n 's/.* dev \([^ ]*\).*/\1/p')" 2>/dev/null || true
  fi
  echo "Off: the Wake button only broadcasts again."
  exit 0
fi

read -r PC_IP PC_MAC < <("$PY" - "$CONF" <<'PY'
import sys, yaml
p = (yaml.safe_load(open(sys.argv[1])) or {}).get("power") or {}
print(str(p.get("pc_ip") or "-"), str(p.get("pc_mac") or "-"))
PY
)
[[ "$PC_IP" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || { echo "Set power.pc_ip in $CONF (the PC's address, e.g. 192.168.8.192)" >&2; exit 1; }
PC_MAC=$(echo "$PC_MAC" | tr -d ':-' | tr 'A-F' 'a-f')
[[ "$PC_MAC" =~ ^[0-9a-f]{12}$ ]] || { echo "Set power.pc_mac in $CONF (the PC's wired card)" >&2; exit 1; }
PC_MAC=$(echo "$PC_MAC" | sed 's/../&:/g; s/:$//')

mkdir -p "$(dirname "$HOOK")"
cat > "$HOOK" <<HOOKEOF
#!/bin/sh
# Argus (deploy/linux/pc-wake-setup.sh): keep the PC's card known, so the Wake button's packet reaches it while it's
# off. Runs when a network comes up on this laptop.
PC_IP=$PC_IP
PC_MAC=$PC_MAC
case "\$2" in up|dhcp4-change|connectivity-change|"") ;; *) exit 0 ;; esac
route=\$(ip -4 route get "\$PC_IP" 2>/dev/null | head -1)
case "\$route" in *" via "*|"") exit 0 ;; esac   # not on a home network that has the PC: nothing to do
dev=\$(echo "\$route" | sed -n 's/.* dev \([^ ]*\).*/\1/p')
case "\$dev" in ""|lo|tailscale*|docker*|br-*|veth*|virbr*) exit 0 ;; esac
ip neigh replace "\$PC_IP" lladdr "\$PC_MAC" dev "\$dev" nud permanent
HOOKEOF
chmod 755 "$HOOK"
chown root:root "$HOOK"
systemctl enable --now NetworkManager-dispatcher.service >/dev/null 2>&1 || true

"$HOOK" "" up
if ip neigh show "$PC_IP" | grep -qi "$PC_MAC.*PERMANENT"; then
  echo "On: $PC_IP is kept as the PC's card ($PC_MAC). The Wake button now also sends straight to it."
  echo "Keep the PC at $PC_IP in your router (a DHCP reservation), or set power.pc_ip again and rerun this."
else
  echo "The laptop isn't on the PC's network right now; it's set up and applies when it is." >&2
fi
