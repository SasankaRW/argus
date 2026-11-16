"""devices.json: the paired devices (id, name, pubkey, addresses). The agent works from it when Argus is down."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path


class DeviceBook:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self.devices: dict[str, dict] = {}
        if path.exists():
            try:
                self.devices = {d["id"]: d for d in json.loads(path.read_text(encoding="utf-8"))}
            except (ValueError, KeyError, TypeError):
                self.devices = {}

    def get(self, device_id: str) -> dict | None:
        return self.devices.get(device_id)

    def name_of(self, device_id: str) -> str:
        d = self.devices.get(device_id)
        return (d or {}).get("name") or device_id

    def update(self, devices: list[dict], keep: str | None = None) -> bool:
        """Merge the list Argus returned. A changed key for a known id is refused (returns False): re-pairing on
        purpose means editing devices.json. `keep` is this device's own id (never overwritten from outside)."""
        ok = True
        with self._lock:
            new = dict(self.devices)
            for d in devices:
                old = new.get(d.get("id"))
                if d.get("id") == keep or not d.get("pubkey"):
                    continue
                if old and old.get("pubkey") != d["pubkey"]:
                    ok = False
                    continue
                new[d["id"]] = {k: d.get(k) for k in ("id", "name", "pubkey", "lan", "tailscale")}
            if new != self.devices:
                self.devices = new
                self._save()
        return ok

    def put(self, device: dict) -> None:
        with self._lock:
            self.devices[device["id"]] = device
            self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(list(self.devices.values()), indent=1), encoding="utf-8")
        os.replace(tmp, self.path)
