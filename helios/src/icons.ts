// The Material Symbols Rounded icons Helios uses (phone and desktop). The font in src/fonts is cut down to exactly these
// (scripts/icons.py): add a name here, then run that script. "check" is also drawn by sonoma.css.
export const ICONS = [
  "home", "graphic_eq", "extension", "hub", "more_horiz", "mic", "bolt", "computer", "pending_actions", "play_arrow",
  "receipt_long", "inbox", "notifications", "queue", "history", "ios_share", "power_settings_new", "memory", "tune",
  "install_mobile", "desktop_windows", "chevron_right", "cloud_off", "refresh", "close", "account_tree", "check",
  "delete", "arrow_back", "restart_alt", "bedtime", "add",
  "neurology", "terminal", "settings", "search", "expand_less", "expand_more", "chevron_left", "hourglass_top", "devices",
  "folder_open", "lock", "send", "fit_screen", "visibility", "dashboard", "keyboard_command_key",
] as const;
export type IconName = (typeof ICONS)[number];
