# Plugins

One folder per plugin: `plugin.yaml` (the manifest) and `plugin.py` (its workflows). See `docs/plugin-guide.md`.
A plugin starts in dry-run; list it under `plugins.live` in argus.yaml to let it change things.

| Plugin | What it does |
| --- | --- |
| downloads-organizer | sorts new files in Downloads into category folders |
| screenshot-renamer | gives new screenshots descriptive names (OCR + T1, or the vision model) |
