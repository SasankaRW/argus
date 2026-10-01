"""Your recordings for training Ari on your voice: data/voice-train/<sentence id>.<ext> plus samples.json (which
sentence each one reads, how long it is). Recorded in Helios, used by `python -m argus.voice_train` on the PC.
They never leave your machines."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

EXTS = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/wav": "wav", "audio/x-wav": "wav", "audio/mp4": "m4a"}


class Samples:
    def __init__(self, folder: Path):
        self.dir = folder
        self._lock = threading.Lock()

    @property
    def index(self) -> Path:
        return self.dir / "samples.json"

    def all(self) -> dict[str, dict]:
        try:
            return json.loads(self.index.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, items: dict[str, dict]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.index.with_suffix(".tmp")
        tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.index)

    def add(self, sid: str, text: str, data: bytes, kind: str, seconds: float) -> dict:
        ext = EXTS.get(kind, "webm")
        with self._lock:
            items = self.all()
            old = items.get(sid)
            if old:
                (self.dir / old["file"]).unlink(missing_ok=True)
            self.dir.mkdir(parents=True, exist_ok=True)
            name = f"{sid}.{ext}"
            (self.dir / name).write_bytes(data)
            items[sid] = {"id": sid, "text": text, "file": name, "seconds": round(float(seconds), 2),
                          "at": int(time.time())}
            self._save(items)
            return items[sid]

    def remove(self, sid: str) -> bool:
        with self._lock:
            items = self.all()
            it = items.pop(sid, None)
            if it is None:
                return False
            (self.dir / it["file"]).unlink(missing_ok=True)
            self._save(items)
            return True

    def path(self, sid: str) -> Path | None:
        it = self.all().get(sid)
        return self.dir / it["file"] if it else None
