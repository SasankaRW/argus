# Notes vault

One Obsidian vault for everything you learn, the same on the PC, the laptop and the phone (Syncthing, over
Tailscale only). Whatever lands in `00 Inbox` is filed under the right topic and linked to related notes; your
words stay as you wrote them. Notes you mark `#enhance` are rewritten by Claude (with sources), your original text
kept folded at the bottom.

```
Vault/
  00 Inbox/            new notes land here (from anywhere); filed ones move to 00 Inbox/_done
  Topics/<Area>/       one note per topic, e.g. Topics/Docker/Image Layers.md
  Maps/<Area> map.md   every topic in an area, one line each
  Sources/             saved links (title, address, a short extract)
  Attachments/         pictures
  Templates/           Quick note, Mark for enhance
  Home.md              start here: the areas
```

## Set up (once)

The laptop keeps the main copy (it's always on, and its old versions are kept 90 days). Each device talks only to
your other devices' Tailscale addresses: Syncthing's discovery and relays are off.

1. **PC** (PowerShell, in the Argus folder):
   ```powershell
   .\scripts\vault-setup.ps1
   ```
   Installs Syncthing (winget), starts it at logon, makes `C:\Users\<you>\Vault`, shares it, and prints this PC's
   **device ID**.
2. **Laptop** (in the Argus folder there):
   ```bash
   deploy/linux/vault-setup.sh
   ```
   Syncthing in Docker, `~/Vault`, prints the laptop's device ID.
3. **Introduce them** (each side once, with the other's ID and Tailscale IP from `tailscale ip -4`):
   ```powershell
   .\scripts\vault-setup.ps1 add <laptop ID> laptop <laptop 100.x.y.z>     # on the PC
   ```
   ```bash
   deploy/linux/vault-setup.sh add <PC ID> pc <PC 100.x.y.z>              # on the laptop
   ```
4. **Phone (Android):**
   - Install **Syncthing-Fork** (F-Droid or IzzyOnDroid) and **Obsidian**.
   - Syncthing-Fork > Settings > Connections: turn off Global discovery, Relays and NAT traversal.
   - Add the laptop and the PC as devices (their IDs; address `tcp://<100.x.y.z>:22000`).
   - On the PC and laptop: `add <phone ID> phone <phone 100.x.y.z>`. Accept the "vault" folder on the phone into a
     folder like `Documents/Vault`, and add the ignore lines from `.stignore` (folder > Ignore patterns).
   - Obsidian > Open folder as vault > that folder.
5. **Argus:** add `vault` to `plugins.live` in argus.yaml (or its Live switch in Helios). For Life Hub's
   `learn: …` notes, `LIFEHUB_API_KEY` must be in Argus's `.env` (the same key the Life Hub plugin uses).

Check any time: `.\scripts\vault-setup.ps1 status` (who is connected, is the vault in step).

## Use it

- **Write in Obsidian:** a new note goes to `00 Inbox` by itself (the vault's setting). Put a topic in the
  Quick note template's `topic:` line if you like.
- **Tell Ari:** "add to my notes on Docker: COPY --chown sets the owner in one layer", "I learned that …",
  "enhance my note on attention", "what did I note about Kubernetes probes?".
- **Share:** a link, text or picture from the phone (Helios > Share > Save to my notes vault). Links are saved under
  `Sources/` with a short extract; the text in pictures is read on the PC (V1).
- **Life Hub:** quick add > Note, or `learn: …` from any tab (`learn!: …` = also enhance). Works offline; it's
  taken into the vault when the laptop is reachable.

**Written up:** what you tell Ari (and what you learned, from your diary) becomes a clean note first: a title, a
one-line gist and bullets, your words folded under "What I said" (`write_spoken`, on). Typed notes stay as written.

**Who writes it:** `model: claude` (default; falls back to the local models when Claude is away or over its daily
budget) or `local` (nothing leaves your machines). The same model picks where notes go.

**Where it goes:** with `filing: auto` (the default) it files by itself and asks (Helios and the phone) only when
unsure or for a brand-new area: area, title, or the title of an existing note to add to. `ask`: every time.

**Enhance:** `#enhance` anywhere in a topic note (or the Mark for enhance template, or `enhance: true`). Within
10 minutes Claude rewrites it: summary, key ideas, explanation, examples, common mistakes, test-yourself
questions, sources. Your text is kept under "My original notes"; anything you add later is merged in when you mark
it again. At most `enhance_per_day` notes a day (10); only the marked note is sent.

## Safe with sync

- A note changed in the last 2 minutes (by you, or a sync that just arrived) is left alone and tried later.
- Existing notes are changed only through `replace_text`: the old version is copied to
  `~/.argus-trash/versions/<date>/` first, and nothing is written if the file changed since it was read.
- If two devices change the same note before they sync, Syncthing keeps both (`… .sync-conflict-….md`); you get one
  phone message per conflict copy. Keep what you want, delete the copy.
