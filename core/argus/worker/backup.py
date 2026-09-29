"""The PC keeps a copy of Argus's nightly backup (backup.copy_to), so losing the laptop's disk loses nothing."""

from __future__ import annotations

import os
import re
from pathlib import Path

from ..backup import Backups
from .workflows import Context, PermanentError, workflow


@workflow("backup", "copy")
def copy(ctx: Context):
    name = str(ctx.input.get("file") or "")
    if not re.fullmatch(r"argus-\d{8}-\d{6}\.db", name):
        raise PermanentError(f"not a backup name: {name!r}")
    folder = Path(os.path.expandvars(os.path.expanduser(str(ctx.input.get("to") or ""))))
    keep = int(ctx.input.get("keep", 5))
    if not str(folder):
        raise PermanentError("no folder to copy to (backup.copy_to)")

    def fetch():
        data = ctx.shared_backup(name)
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / (name + ".part")
        tmp.write_bytes(data)
        check = Backups.verify(tmp)
        if not check["ok"]:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"the copy failed its check: {check.get('problem')}")  # retried
        tmp.replace(folder / name)
        old = sorted(folder.glob("argus-*.db"))[:-keep]
        for p in old:
            p.unlink(missing_ok=True)
        return {"saved": str(folder / name), "size": len(data), "jobs": check["jobs"], "removed": [p.name for p in old]}

    return ctx.step("copy", fetch)
