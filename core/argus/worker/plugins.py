"""The plugin host (worker side): import each plugin's code and give its jobs a ctx limited to its manifest.

What a plugin may do comes from its plugin.yaml, checked here on every call:

- ctx.files: read only under `permissions.files.read` (and write), change only under `write`; nothing outside
  the argus.yaml `paths.allowed` list (when set) or inside `paths.blocked`, ever. Paths are resolved first, so
  `..` and links can't step outside. No permanent deletes: `recycle()` needs `delete: recycle_bin` and uses the
  Recycle Bin (or ~/.argus-trash when there is none). Moves never overwrite. Every change is an event (the undo
  log), and in dry-run nothing changes: it only says what it would do.
- ctx.http: only the hosts in `permissions.network`.
- ctx.secrets: only the names in `permissions.secrets`, read from this machine's .env or environment.
- ctx.llm: starts at a tier the manifest lists (higher ones only by escalation); Claude calls count against
  `claude_calls_per_day` in argusd.
"""

from __future__ import annotations

import fnmatch
import importlib.util
import json
import logging
import os
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from ..config import parse_env_file
from .workflows import REGISTRY, PermanentError, WorkflowRegistry

log = logging.getLogger("argus.plugins")


class PermissionDenied(PermanentError):  # noqa: N818 - reads naturally at the call site
    """The plugin asked for something its manifest does not allow. Retrying won't help: the job goes dead."""


def _expand(p: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(p))).resolve()


def _within(path: Path, roots: list[Path]) -> bool:
    return any(path == r or r in path.parents for r in roots)


class Files:
    def __init__(self, plugin: str, perms: dict, rules: dict, dry_run: bool, trace):
        self.plugin = plugin
        self.read_roots = [_expand(p) for p in perms.get("read", [])]
        self.write_roots = [_expand(p) for p in perms.get("write", [])]
        self.delete = perms.get("delete", "none")
        self.allowed = [_expand(p) for p in rules.get("allowed", [])]
        self.blocked = [_expand(p) for p in rules.get("blocked", [])]
        self.dry_run = dry_run
        self._trace = trace

    def _check(self, path: str | Path, write: bool) -> Path:
        p = _expand(str(path))
        if _within(p, self.blocked):
            raise PermissionDenied(f"{p} is in a blocked folder")
        if self.allowed and not _within(p, self.allowed):
            raise PermissionDenied(f"{p} is outside the folders Argus may touch (paths.allowed)")
        roots = self.write_roots if write else self.read_roots + self.write_roots
        if not _within(p, roots):
            kind = "write" if write else "read"
            raise PermissionDenied(f"{self.plugin} may not {kind} {p} (add it under permissions.files.{kind})")
        return p

    # -------------------------------------------------------------- reading

    def read_text(self, path: str | Path, encoding: str = "utf-8", limit: int | None = None) -> str:
        """The file's text (the first `limit` characters when given)."""
        p = self._check(path, False)
        if limit is None:
            return p.read_text(encoding=encoding, errors="replace")
        with open(p, encoding=encoding, errors="replace") as f:
            return f.read(limit)

    def read_bytes(self, path: str | Path) -> bytes:
        return self._check(path, False).read_bytes()

    def exists(self, path: str | Path) -> bool:
        return self._check(path, False).exists()

    def stat(self, path: str | Path) -> os.stat_result:
        return self._check(path, False).stat()

    def is_dir(self, path: str | Path) -> bool:
        return self._check(path, False).is_dir()

    def list(self, folder: str | Path, pattern: str = "*") -> list[str]:
        d = self._check(folder, False)
        return sorted(str(p) for p in d.iterdir() if fnmatch.fnmatch(p.name, pattern)) if d.is_dir() else []

    # -------------------------------------------------------------- changing (undo log; dry-run aware)

    def _free(self, dst: Path) -> Path:
        """Never overwrite: "name (1).ext", "name (2).ext", ..."""
        if not dst.exists():
            return dst
        for i in range(1, 1000):
            cand = dst.with_name(f"{dst.stem} ({i}){dst.suffix}")
            if not cand.exists():
                return cand
        raise PermissionDenied(f"too many files named like {dst}")

    def write_text(self, path: str | Path, text: str, encoding: str = "utf-8") -> str:
        p = self._check(path, True)
        target = self._free(p)
        self._trace("file.written", {"path": str(target), "dry_run": self.dry_run})
        if not self.dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding=encoding)
        return str(target)

    def move(self, src: str | Path, dst: str | Path) -> str:
        """Move (or rename) a file. Both ends must be writable; an existing target is never replaced."""
        s = self._check(src, True)
        d = self._check(dst, True)
        if d.is_dir():
            d = d / s.name
        target = self._free(d)
        self._trace("file.moved", {"from": str(s), "to": str(target), "dry_run": self.dry_run})
        if not self.dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(s), str(target))
        return str(target)

    def recycle(self, path: str | Path) -> None:
        """Send a file to the Recycle Bin: restorable, never a permanent delete."""
        if self.delete != "recycle_bin":
            raise PermissionDenied(f"{self.plugin} may not delete files (permissions.files.delete is none)")
        p = self._check(path, True)
        self._trace("file.recycled", {"path": str(p), "dry_run": self.dry_run})
        if self.dry_run:
            return
        try:
            from send2trash import send2trash  # type: ignore[import-not-found]

            send2trash(str(p))
        except ImportError:  # no Recycle Bin support installed: a dated folder in the home directory instead
            bin_ = Path.home() / ".argus-trash" / time.strftime("%Y-%m-%d")
            bin_.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p), str(self._free(bin_ / p.name)))


class Http:
    def __init__(self, plugin: str, hosts: list[str], trace, timeout: float = 15):
        self.plugin = plugin
        self.hosts = {h.lower() for h in hosts}
        self._trace = trace
        self.timeout = timeout

    def _check(self, url: str) -> str:
        u = urllib.parse.urlparse(url)
        if u.scheme not in ("http", "https") or (u.hostname or "").lower() not in self.hosts:
            raise PermissionDenied(f"{self.plugin} may not call {u.hostname!r} (add it under permissions.network)")
        return url

    def request(self, method: str, url: str, *, json_body: Any = None,
                headers: dict | None = None) -> tuple[int, bytes]:
        self._check(url)
        data = json.dumps(json_body).encode() if json_body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
        if data is not None:
            req.add_header("Content-Type", "application/json")
        self._trace("http.request", {"method": method, "host": urllib.parse.urlparse(url).hostname})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def get_json(self, url: str, headers: dict | None = None) -> Any:
        status, body = self.request("GET", url, headers=headers)
        if status >= 400:
            raise RuntimeError(f"GET {url}: HTTP {status}")
        return json.loads(body)


class Secrets:
    def __init__(self, plugin: str, names: list[str]):
        self.plugin = plugin
        self.names = set(names)
        self._env = parse_env_file(Path(".env"))

    def __getitem__(self, name: str) -> str:
        if name not in self.names:
            raise PermissionDenied(f"{self.plugin} may not read secret {name} (add it under permissions.secrets)")
        v = os.environ.get(name) or self._env.get(name)
        if not v:
            raise KeyError(f"{name} is not set in .env")
        return v

    def get(self, name: str, default: str | None = None) -> str | None:
        try:
            return self[name]
        except KeyError:
            return default


class Store:
    """The plugin's own small key-value store in argusd (state between runs)."""

    def __init__(self, client, plugin: str):
        self.client = client
        self.plugin = plugin

    def get(self, key: str, default: Any = None) -> Any:
        v = self.client.get(f"/plugins/{self.plugin}/state/{urllib.parse.quote(key, safe='')}")["value"]
        return default if v is None else v

    def set(self, key: str, value: Any) -> None:
        self.client.call("PUT", f"/plugins/{self.plugin}/state/{urllib.parse.quote(key, safe='')}",
                         {"value": value})


class LoadedPlugin:
    def __init__(self, info: dict[str, Any]):
        self.info = info
        self.id: str = info["id"]
        self.live: bool = info.get("live", False)
        self.config: dict[str, Any] = info.get("config") or {}
        self.perms: dict[str, Any] = info.get("permissions") or {}

    @property
    def dry_run(self) -> bool:
        return not self.live


def load(infos: list[dict[str, Any]], registry: WorkflowRegistry | None = None) -> tuple[dict[str, LoadedPlugin],
                                                                                         dict[str, str]]:
    """Import each plugin's plugin.py. Returns ({id: plugin}, {id: error}). A broken plugin is skipped, never fatal."""
    reg = registry or REGISTRY
    loaded: dict[str, LoadedPlugin] = {}
    errors: dict[str, str] = {}
    for info in infos:
        pid, path = info["id"], Path(info["path"]) / "plugin.py"
        if not path.exists():
            errors[pid] = f"{path} not found on this machine"
            continue
        name = "argus_plugin_" + pid.replace("-", "_")
        for r in {id(REGISTRY): REGISTRY, id(reg): reg}.values():  # a reload replaces the old workflows
            for k in [k for k in r.items if k[0] == pid]:
                r.items.pop(k)
        try:
            before = set(REGISTRY.items)
            spec = importlib.util.spec_from_file_location(name, path)
            assert spec and spec.loader
            mod = importlib.util.module_from_spec(spec)
            sys.modules[name] = mod
            spec.loader.exec_module(mod)
        except Exception as e:
            errors[pid] = f"plugin.py failed to import: {type(e).__name__}: {e}"
            log.error("plugin failed to load", extra={"plugin": pid, "error": errors[pid]})
            continue
        added = {k: v for k, v in REGISTRY.items.items() if k not in before}
        wrong = [f"{p}.{w}" for (p, w) in added if p != pid]
        missing = [w for w in info.get("workflows", []) if (pid, w) not in {**added, **REGISTRY.items}]
        if wrong or missing:
            errors[pid] = ("registers workflows for another plugin: " + ", ".join(wrong)) if wrong else \
                ("does not register workflow(s): " + ", ".join(missing))
            for k in added:
                REGISTRY.items.pop(k, None)
            continue
        if reg is not REGISTRY:
            for wf in added.values():
                reg.add(wf)
        loaded[pid] = LoadedPlugin(info)
        log.info("plugin loaded", extra={"plugin": pid, "live": info.get("live"), "workflows": info.get("workflows")})
    return loaded, errors
