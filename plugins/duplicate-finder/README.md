# Duplicate finder

Finds files with identical content (whatever their names) in the folders you choose and, after one approval,
sends the extra copies to the Recycle Bin. Sunday 4 am, or **Find duplicates** in Helios.

- Duplicates are decided by content only: same size, same first 64 KB, same SHA-256.
- The copy that stays: outside Downloads, with a plain name (not "photo (1).jpg"), in the shallowest folder, the
  oldest.
- Nothing is removed without your yes, and each file is checked again right before; everything goes to the
  Recycle Bin.
- Settings (Helios): `folders` (compared together), `min_size_kb`, `max_groups` per approval.
- Dry-run until you add `duplicate-finder` to `plugins.live`.
