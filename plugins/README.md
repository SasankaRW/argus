# Plugins

One folder per plugin: `plugin.yaml` (the manifest) and `plugin.py` (its workflows). See `docs/plugin-guide.md`.
A plugin starts in dry-run; list it under `plugins.live` in argus.yaml to let it change things.

| Plugin | What it does |
| --- | --- |
| downloads-organizer | sorts new files in Downloads into category folders |
| screenshot-renamer | gives new screenshots descriptive names (OCR + T1, or the vision model) |
| vault | your Obsidian vault (~/Vault, synced with Syncthing over Tailscale): Inbox notes from Obsidian, Ari, the share menu and Life Hub filed under the right topic and linked; notes marked #enhance improved by Claude (see vault/README.md) |
| eyes | makes a robot-eyes clip for each app you use on the cooler's screen (Claude, in a studio folder, checked by Argus before the player gets it) |
| advisor | Life Hub's money advisor: advice in words from Life Hub's numbers (local or Claude), cheaper options for wishlist items, the monthly PDF report |
