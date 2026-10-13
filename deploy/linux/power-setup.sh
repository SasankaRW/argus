#!/usr/bin/env bash
# The laptop's power buttons in Helios (restart Argus, restart / shut down the laptop): allow exactly these to the
# argus user through polkit. argusd runs with NoNewPrivileges, so sudo can't be used; systemd and logind ask
# polkit instead. Run once (and again after an Argus reinstall):  sudo bash /opt/argus/deploy/linux/power-setup.sh
#   off:  sudo bash /opt/argus/deploy/linux/power-setup.sh off
set -euo pipefail
RULE=/etc/polkit-1/rules.d/50-argus-power.rules
if [ "${1:-}" = "off" ]; then
  rm -f "$RULE"
  echo "Laptop power buttons off (the PC's still work)."
  exit 0
fi
if ! command -v pkaction >/dev/null 2>&1; then
  if command -v pacman >/dev/null 2>&1; then pacman -S --needed --noconfirm polkit
  elif command -v apt-get >/dev/null 2>&1; then apt-get install -y polkitd || apt-get install -y policykit-1
  fi
fi
mkdir -p /etc/polkit-1/rules.d
cat > "$RULE" <<'RULES'
// Argus (deploy/linux/power-setup.sh): the argus user may restart its own services and reboot or power off the
// laptop (Helios > Power). Nothing else.
polkit.addRule(function (action, subject) {
  if (subject.user != "argus") return polkit.Result.NOT_HANDLED;
  var power = ["org.freedesktop.login1.reboot", "org.freedesktop.login1.reboot-multiple-sessions",
               "org.freedesktop.login1.reboot-ignore-inhibit", "org.freedesktop.login1.power-off",
               "org.freedesktop.login1.power-off-multiple-sessions",
               "org.freedesktop.login1.power-off-ignore-inhibit"];
  if (power.indexOf(action.id) >= 0) return polkit.Result.YES;
  if (action.id == "org.freedesktop.systemd1.manage-units" &&
      ["argusd.service", "argus-worker.service"].indexOf(action.lookup("unit")) >= 0 &&
      action.lookup("verb") == "restart") return polkit.Result.YES;
  return polkit.Result.NOT_HANDLED;
});
RULES
chmod 644 "$RULE"
systemctl try-restart polkit 2>/dev/null || true
echo "Laptop power buttons on: Helios > Power > Laptop (restart Argus, restart, shut down, cancel)."
