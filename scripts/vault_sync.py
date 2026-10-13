"""Set up Syncthing for the notes vault, on any machine (the PC, the laptop), through Syncthing's local REST API.

    python scripts/vault_sync.py setup --vault ~/Vault [--folder-path /var/syncthing/Vault]
    python scripts/vault_sync.py add --id <DEVICE-ID> --name laptop --address 100.101.102.103
    python scripts/vault_sync.py status

setup: copies the starter vault into --vault (only files that aren't there yet), shares it as the Syncthing folder
"vault" (send and receive, file versioning kept 90 days), and keeps Syncthing on your own network: global
discovery, relays and NAT traversal off, so devices only meet at the Tailscale addresses you give with `add`.
Prints this machine's device ID for the other devices.

add: another device of yours (its ID from its own `setup` / the Syncthing app), at its Tailscale address, and shares
the vault with it.

The API key is read from Syncthing's config.xml (or --api-key); --gui is where Syncthing listens (127.0.0.1:8384).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

FOLDER = "vault"
STARTER = Path(__file__).resolve().parents[1] / "plugins" / "vault" / "starter"
KEEP_VERSIONS_S = 90 * 86400
DEVICE_ID = re.compile(r"^[A-Z0-9]{7}(-[A-Z0-9]{7}){7}$")


def config_candidates() -> list[Path]:
    home = Path.home()
    out = []
    if os.environ.get("LOCALAPPDATA"):
        out.append(Path(os.environ["LOCALAPPDATA"]) / "Syncthing" / "config.xml")
    out += [home / ".local" / "state" / "syncthing" / "config.xml", home / ".config" / "syncthing" / "config.xml",
            Path("data") / "config" / "config.xml", Path("syncthing") / "config" / "config.xml"]
    return out


def api_key(config: str | None) -> str:
    paths = [Path(config)] if config else config_candidates()
    for p in paths:
        if p.is_file():
            m = re.search(r"<apikey>([^<]+)</apikey>", p.read_text(encoding="utf-8", errors="replace"))
            if m:
                return m.group(1).strip()
    raise SystemExit("Syncthing's API key not found: is Syncthing running? (or pass --config / --api-key)")


class Api:
    def __init__(self, gui: str, key: str):
        self.base = gui if "://" in gui else f"http://{gui}"
        self.key = key

    def call(self, method: str, path: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base.rstrip("/") + path, data=data, method=method,
                                     headers={"X-API-Key": self.key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            raise SystemExit(f"Syncthing said HTTP {e.code} for {method} {path}: {e.read()[:300]!r}") from None
        except OSError as e:
            raise SystemExit(f"Syncthing isn't answering at {self.base} ({e})") from None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return raw.decode(errors="replace")

    def folder(self) -> dict | None:
        folders = self.call("GET", "/rest/config/folders") or []
        return next((f for f in folders if f.get("id") == FOLDER), None)


def copy_starter(vault: Path) -> list[str]:
    """The starter files that aren't in the vault yet (nothing you have is replaced)."""
    added = []
    for src in sorted(STARTER.rglob("*")):
        if src.is_dir():
            continue
        dst = vault / src.relative_to(STARTER)
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            added.append(src.relative_to(STARTER).as_posix())
    for d in ("00 Inbox", "Topics", "Maps", "Sources", "Attachments"):
        (vault / d).mkdir(parents=True, exist_ok=True)
    return added


def setup(api: Api, vault: Path, folder_path: str) -> dict:
    added = copy_starter(vault)
    api.call("PATCH", "/rest/config/options", {"globalAnnounceEnabled": False, "relaysEnabled": False,
                                               "natEnabled": False, "localAnnounceEnabled": True,
                                               "urAccepted": -1})
    me = api.call("GET", "/rest/system/status")["myID"]
    versioning = {"type": "staggered", "params": {"maxAge": str(KEEP_VERSIONS_S), "cleanInterval": "3600"},
                  "cleanupIntervalS": 3600}
    have = api.folder()
    if have is None:
        api.call("PUT", f"/rest/config/folders/{FOLDER}", {
            "id": FOLDER, "label": "Vault", "path": folder_path, "type": "sendreceive",
            "devices": [{"deviceID": me}], "fsWatcherEnabled": True, "rescanIntervalS": 3600,
            "versioning": versioning})
    else:
        api.call("PATCH", f"/rest/config/folders/{FOLDER}", {"path": folder_path, "type": "sendreceive",
                                                             "versioning": versioning})
    return {"device_id": me, "starter_added": added, "folder": folder_path, "existed": have is not None}


def add(api: Api, device: str, name: str, address: str) -> dict:
    device = device.strip().upper()
    if not DEVICE_ID.match(device):
        raise SystemExit("that isn't a Syncthing device ID (8 groups of 7 letters/digits, joined by -)")
    host = address.strip()
    if not re.fullmatch(r"100\.\d{1,3}\.\d{1,3}\.\d{1,3}|[a-z0-9-]+(\.[a-z0-9-]+)*\.ts\.net", host):
        raise SystemExit("use the device's Tailscale address (100.x.y.z or name.tailnet.ts.net)")
    api.call("PUT", f"/rest/config/devices/{device}", {
        "deviceID": device, "name": name, "addresses": [f"tcp://{host}:22000", f"quic://{host}:22000"],
        "autoAcceptFolders": False, "introducer": False})
    f = api.folder()
    if f is None:
        raise SystemExit("run `setup` first: there's no vault folder in Syncthing yet")
    ids = {d.get("deviceID") for d in f.get("devices") or []}
    if device not in ids:
        api.call("PATCH", f"/rest/config/folders/{FOLDER}",
                 {"devices": [*(f.get("devices") or []), {"deviceID": device}]})
    return {"added": name, "device_id": device, "addresses": [f"tcp://{host}:22000", f"quic://{host}:22000"]}


def status(api: Api) -> dict:
    me = api.call("GET", "/rest/system/status")["myID"]
    devices = {d["deviceID"]: d.get("name") or d["deviceID"][:7] for d in api.call("GET", "/rest/config/devices")}
    conns = (api.call("GET", "/rest/system/connections") or {}).get("connections") or {}
    out = {"device_id": me, "devices": {}}
    for did, name in devices.items():
        if did != me:
            c = conns.get(did) or {}
            out["devices"][name] = "connected" if c.get("connected") else "not connected"
    if api.folder() is not None:
        st = api.call("GET", f"/rest/db/status?folder={FOLDER}") or {}
        out["vault"] = {"state": st.get("state"), "files": st.get("localFiles"), "need": st.get("needFiles")}
    else:
        out["vault"] = "not set up (run setup)"
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--gui", default="127.0.0.1:8384", help="where Syncthing listens")
    ap.add_argument("--config", help="Syncthing's config.xml (for the API key)")
    ap.add_argument("--api-key")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup")
    s.add_argument("--vault", default="~/Vault", help="the vault folder on this machine")
    s.add_argument("--folder-path", help="the same folder as Syncthing sees it (Docker), default --vault")
    a = sub.add_parser("add")
    a.add_argument("--id", required=True)
    a.add_argument("--name", required=True)
    a.add_argument("--address", required=True, help="its Tailscale IP (100.x.y.z) or name.tailnet.ts.net")
    sub.add_parser("status")
    args = ap.parse_args(argv)
    api = Api(args.gui, args.api_key or api_key(args.config))
    if args.cmd == "setup":
        vault = Path(os.path.expanduser(args.vault))
        out = setup(api, vault, args.folder_path or str(vault))
        print(json.dumps(out, indent=2))
        print(f"\nThis machine's device ID (add it on your other devices):\n  {out['device_id']}")
    elif args.cmd == "add":
        print(json.dumps(add(api, args.id, args.name, args.address), indent=2))
    else:
        print(json.dumps(status(api), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
