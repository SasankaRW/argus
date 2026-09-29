# downloads-organizer

Sorts what lands in `~/Downloads` into category folders (Documents, Images, Projects, ...). Port of the standalone
script in `C:\Users\Sas\projects\downloads-organizer`.

- **When:** a new file that has stopped changing for 2 minutes (folder watch on the PC worker), a nightly sweep at
  03:30, and the "Sort Downloads now" button on its Helios box.
- **How it decides:** siblings (`_v2`, `(1)`, dates) are grouped; a group whose name matches a folder already inside a
  category goes there with no model; otherwise T1 picks the category (T2 if T1's answer isn't a real category);
  the extension only when no model answers. Two or more siblings get their own subfolder.
- **Safety:** only `~/Downloads` (root files only), moves never overwrite, no deletes, every move is an event. It
  starts in dry-run: it records what it would do and moves nothing until you add `downloads-organizer` under
  `plugins.live` in argus.yaml.
- **Tuning:** `rules.yaml` (categories, guidance, examples, extension fallback). Restart the worker after a change.
  Settings (folder, limits) under `plugins.config.downloads-organizer` in argus.yaml.
- **Undo:** `POST /plugins/downloads-organizer/run {"workflow": "undo", "input": {"from": "<now>", "to": "<before>"}}`
  (a button in Helios comes with the runs inspector).

Going live: turn off the old script's scheduled task first (`uninstall-schedule.bat` in its folder), so the two
don't both sort.
