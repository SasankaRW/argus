#!/usr/bin/env bash
# Argus console on the laptop: one terminal window with the Argus logo and eye, Ari's conversation, events and logs,
# opened by itself when Hyprland starts. Run once as your normal user (not root), on the laptop:
#   bash /opt/argus/deploy/linux/console-setup.sh            # set up (safe to run again)
#   bash /opt/argus/deploy/linux/console-setup.sh off        # stop opening it at login (the launcher stays)
# Optional: ARGUS_CONSOLE_WORKSPACE=9 (the Hyprland workspace it opens on, without taking your focus).
set -euo pipefail
DIR=${ARGUS_DIR:-/opt/argus}
PY="$DIR/.venv/bin/python"
WS=${ARGUS_CONSOLE_WORKSPACE:-9}
CONF=${XDG_CONFIG_HOME:-$HOME/.config}
HYPR="$CONF/hypr/hyprland.conf"
LAUNCH="$HOME/.local/bin/argus-console"
MARK="# argus console"

say() { printf '\n\033[33m== %s\033[0m\n' "$*"; }

if [ "${1:-}" = "off" ]; then
  [ -f "$HYPR" ] && sed -i "/$MARK/,+1d" "$HYPR"
  echo "Hyprland no longer opens it at login. Open it by hand any time: argus-console"
  exit 0
fi

[ "$(id -u)" -ne 0 ] || { echo "run as your normal user, not root (it uses sudo)"; exit 1; }

say "packages"
sudo pacman -S --needed --noconfirm tmux kitty ttf-jetbrains-mono >/dev/null

say "python bits (rich, psutil)"
sudo -H -u argus "$DIR/.venv/bin/pip" install --quiet -e "$DIR[console]"

say "token (so the console can read argusd)"
mkdir -p "$CONF/argus"
tok=$(sudo grep -E '^ARGUS_WORKER_TOKEN=' "$DIR/.env" | head -1 | cut -d= -f2- || true)
(umask 077; printf 'ARGUS_WORKER_TOKEN=%s\n' "$tok" > "$CONF/argus/console.env")
[ -n "$tok" ] || echo "(no worker token in $DIR/.env: the console will only work if argusd needs none)"

say "terminal look"
cat > "$CONF/argus/console-kitty.conf" <<'KITTY'
font_family JetBrains Mono
font_size 11.0
background #0b0f14
background_opacity 0.82
dynamic_background_opacity yes
foreground #e5e7eb
cursor #5eead4
cursor_blink_interval 0
window_padding_width 4
confirm_os_window_close 0
enable_audio_bell no
hide_window_decorations yes
remember_window_size no
initial_window_width 200c
initial_window_height 55c
KITTY

say "launcher"
mkdir -p "$(dirname "$LAUNCH")"
cat > "$LAUNCH" <<LAUNCHER
#!/usr/bin/env bash
# Opens the Argus console in its own window (class argus-console).
exec kitty --config "$CONF/argus/console-kitty.conf" --class argus-console --title "Argus" \\
  "$PY" -m argus.console all
LAUNCHER
chmod +x "$LAUNCH"

say "Hyprland: open it at login"
if [ ! -f "$HYPR" ]; then
  echo "no $HYPR found: add this line to your Hyprland config yourself:"
  echo "  exec-once = [workspace $WS silent] $LAUNCH"
elif grep -q "$MARK" "$HYPR"; then
  echo "already in $HYPR"
else
  printf '\n%s (console-setup.sh; remove with: console-setup.sh off)\nexec-once = [workspace %s silent] %s\n' \
    "$MARK" "$WS" "$LAUNCH" >> "$HYPR"
  echo "added to $HYPR: it opens on workspace $WS at login (SUPER+$WS to look at it)"
fi

say "done"
echo "Open it now: argus-console   (or log out and in, or: hyprctl dispatch exec \"[workspace $WS silent] $LAUNCH\")"
echo "Click a pane to focus it; scroll with the mouse wheel; Ctrl+b then d leaves it running in the background."
