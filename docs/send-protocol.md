# Argus Send: protocol contract (v1)

Plan: https://claude.ai/artifact/DQLMVJstRFS2EauLgQ6Zo3. This file is the contract every implementation follows (Python on PC/Calypso, Kotlin on the phone). Change it here first.

## Parts
- **Send agent** on every device, independent of Argus: receiver on TCP 8610, sender, disk queue (SQLite), saved devices + keys, LAN announcement, path switching.
- **Argus** (optional helper, on Calypso): pairing, fresh addresses, shared history, holding area for offline targets.

## Devices and keys
- Device ids: `pc`, `calypso`, `phone` (display: "PC", "Calypso", "my phone").
- Each agent makes an Ed25519 key pair on first start (private key in the agent's data dir, never in the repo).
- Pairing via Argus: `POST /send/devices` (worker token) registers `{id, name, pubkey, lan: [ip:port], tailscale: "<magicdns name>"}`; `GET /send/devices` returns all. Agent saves the list to `devices.json` and works from it when Argus is down. Re-register every 30 s while Argus is up (addresses stay fresh).
- LAN discovery: mDNS/DNS-SD `_argus-send._tcp`, TXT `id=<device id>`. Used when the saved LAN address fails.
- No hostnames, IPs or tokens in code: they come from config/devices.json.

## Auth (every request)
- `POST /send/hello {from, nonce_c}` → `{nonce_s, sig}` (receiver signs `nonce_c`); sender replies inside the next call with `sig` over `nonce_s`. Both check the other's pubkey from devices.json; unknown key → 403.
- Key exchange: each side also sends a fresh X25519 public key (`eph_c` in the hello, `eph_s` in the reply), and the Ed25519 signatures cover `nonce || eph` so the exchange can't be swapped. Session key `K = HKDF-SHA256(X25519(eph), salt=nonce_c||nonce_s, info="argus-send v1"||both device pubkeys sorted bytewise; MAC = HMAC(K, METHOD|path|query|sha256hex(body)|seq); receiver signs nonce_c||eph_s, sender proof signs nonce_s||eph_c; test vectors in docs/send-test-vectors.json)` (32 bytes). A LAN sniffer sees only public values and cannot compute K. Session id `sid` comes with the reply, valid 10 min, renewed by a new hello.
- Replay: each request also carries `X-Send-Seq` (a counter starting at 1 per session, inside the MAC); the receiver refuses a seq it has already seen.
- Every later request carries `X-Send-Session: <sid>` and `X-Send-Mac: HMAC-SHA256(K, method|path|query|sha256(body))`. Chunk bodies: MAC covers the chunk's sha256. Bad/missing MAC → 401.

## Transfer
- `POST /send/offers` body `{offer_id (uuid from sender), from, files: [{n, rel_path, size, mtime, sha256}], total}` → `{have: {n: [chunk indexes already stored]}, done: [n already complete by same sha256]}`. Receiver refuses with 507 + `{missing_bytes}` if free space < total.
- `PUT /send/offers/{offer_id}/files/{n}?chunk=<i>` raw body, chunk size 8 MiB (last may be shorter), header `X-Chunk-Sha256`. Receiver writes into `<root>/.incoming/<offer_id>/<n>.part` at `i*8MiB`, records the chunk, 200. Idempotent.
- `POST /send/offers/{offer_id}/done` → receiver hashes each file, compares sha256; mismatch → 409 `{bad: {n: [chunks]}}`; ok → moves files to the final folder (rules below), 200 `{saved: [paths]}`. Sender deletes the queue entry only after this 200.
- `GET /send/offers/{offer_id}` → same shape as the offers reply (resume after a break).
- Parallelism: up to 4 chunk PUTs at once per file. Many small files: offer them together; files < 1 MiB may be sent as one chunk each in parallel.

## Path choice and switching
1. Try saved LAN address and mDNS and Tailscale name at the same time (hello); first good LAN answer wins, else Tailscale.
2. While on Tailscale, re-try LAN hello every 5 s; on success, finish the chunk in flight and send remaining chunks over LAN. Same in reverse if LAN fails.
3. If neither answers and Argus is reachable: hand the whole share to Calypso's holding area (it is just another receiver with `hold_for=<target>` on the offer). Calypso delivers it later like any sender; if the sender reaches the target directly first, it sends directly and tells Calypso `DELETE /send/holding/{offer_id}`.
4. Else: stay queued.

## Queue (sender, SQLite `send-queue.db` next to the agent data)
- States: `queued → sending → verifying → done` | `held` (handed to Calypso) | `failed` (permanent, with reason) | `cancelled`.
- Written before any byte is sent. Retry triggers: network change, mDNS announce of the target, Tailscale shows it online, Argus reachable again, 30 s timer.
- A queued share waits 7 days, then the sender asks "keep waiting?". Source file changed/deleted while queued → reported, not sent.
- Mobile data (phone): shares over 200 MB wait for Wi-Fi unless the user taps "Send anyway".

## Receiving and folders
- PC `%USERPROFILE%\Argus Shared\From <sender name>\`, Calypso `~/Argus Shared/From <sender name>/`, phone `Download/Argus/From <sender name>/`.
- Folders keep structure and mtimes. Existing name → `name (2).ext`; same sha256 already there → not saved again. Send never deletes user files.
- Calypso holding area: `~/Argus Shared/.holding/`, max 20 GB total, 7 days per share, then the sender is told it was not delivered.

## History and Argus
- Agent keeps local history (last 200). When Argus is reachable it POSTs events to `POST /send/events` (worker token); offline events wait in an outbox. Argus shows them in Helios and lights the map line.

## Picked decisions
1. Plain HTTP on the home LAN (identified by its router's MAC/SSID set in config); Tailscale elsewhere (already encrypted). MAC on every request either way.
2. Folder `Argus Shared` (not Downloads).
3. Queue: 7 days then ask.
4. Mobile data: hold >200 MB until Wi-Fi, "Send anyway".
5. Calypso holds up to 20 GB, 7 days per share.
