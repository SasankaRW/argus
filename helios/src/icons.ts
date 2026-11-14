// The Material Symbols Rounded icons the phone view uses. The font in src/fonts is cut down to exactly these
// (scripts/icons.py): add a name here, then run that script. "check" is also drawn by sonoma-phone.css.
export const ICONS = [
  "home", "graphic_eq", "extension", "hub", "more_horiz", "mic", "bolt", "computer", "pending_actions", "play_arrow",
  "receipt_long", "inbox", "notifications", "queue", "history", "ios_share", "power_settings_new", "memory", "tune",
  "install_mobile", "desktop_windows", "chevron_right", "cloud_off", "refresh", "close", "account_tree", "check",
  "delete", "arrow_back", "restart_alt", "bedtime", "add",
] as const;
export type IconName = (typeof ICONS)[number];
