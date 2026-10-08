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
import ipaddress
import json
import logging
import os
import shutil
import socket
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
        self._gone: set[Path] = set()  # dry-run: what this job would have moved away already

    def _check(self, path: str | Path, write: bool) -> Path:
        p = Path(path).resolve()  # a file's own name is taken literally (no $VAR or ~ expansion)
        if _within(p, self.blocked):
            raise PermissionDenied(f"{p} is in a blocked folder")
        if self.allowed and not _within(p, self.allowed):
            raise PermissionDenied(f"{p} is outside the folders Argus may touch (paths.allowed)")
        roots = self.write_roots if write else self.read_roots + self.write_roots
        if not _within(p, roots):
            kind = "write" if write else "read"
            raise PermissionDenied(f"{self.plugin} may not {kind} {p} (add it under permissions.files.{kind})")
        return p

    def visible(self, path: str | Path) -> bool:
        """May Argus show this path's name at all (argus.yaml paths.blocked / paths.allowed)? For listings that
        come from elsewhere (a search index), whatever the plugin's own read list."""
        try:
            p = Path(path).resolve()
        except (OSError, ValueError):
            return False
        return not _within(p, self.blocked) and (not self.allowed or _within(p, self.allowed))

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

    def walk(self, folder: str | Path, *, limit: int = 50_000, skip_hidden: bool = True,
             skip_dirs: set[str] | frozenset[str] = frozenset()) -> list[tuple[str, int, float]]:
        """Every file under `folder` (subfolders too): (path, size, modified). Links are not followed; folders the
        plugin may not read (paths.blocked) are left out. At most `limit` files."""
        root = self._check(folder, False)
        out: list[tuple[str, int, float]] = []
        if not root.is_dir():
            return out
        todo = [root]
        while todo and len(out) < limit:
            d = todo.pop()
            try:
                entries = list(os.scandir(d))
            except OSError:
                continue
            for e in entries:
                if skip_hidden and (e.name.startswith(".") or e.name.lower() in ("desktop.ini", "thumbs.db")):
                    continue
                p = Path(e.path)
                if _within(p, self.blocked):
                    continue
                try:
                    if e.is_symlink():
                        continue
                    if e.is_dir():
                        if e.name not in skip_dirs:
                            todo.append(p)
                    elif e.is_file():
                        st = e.stat()
                        out.append((str(p), st.st_size, st.st_mtime))
                except OSError:
                    continue
                if len(out) >= limit:
                    break
        return out

    def sha256(self, path: str | Path, limit: int | None = None) -> str:
        """The file's SHA-256, read in pieces (fine for big videos). `limit`: only the first `limit` bytes."""
        import hashlib

        p = self._check(path, False)
        h = hashlib.sha256()
        left = limit
        with open(p, "rb") as f:
            while True:
                chunk = f.read(1 << 20 if left is None else min(1 << 20, left))
                if not chunk:
                    break
                h.update(chunk)
                if left is not None:
                    left -= len(chunk)
                    if left <= 0:
                        break
        return h.hexdigest()

    # -------------------------------------------------------------- changing (undo log; dry-run aware)

    def _free(self, dst: Path) -> Path:
        """Never overwrite: "name (1).ext", "name (2).ext", ... Each candidate is checked like the asked path."""
        if not dst.exists() and not dst.is_symlink():
            return dst
        for i in range(1, 1000):
            cand = self._check(dst.with_name(f"{dst.stem} ({i}){dst.suffix}"), True)
            if not cand.exists() and not cand.is_symlink():
                return cand
        raise PermissionDenied(f"too many files named like {dst}")

    def _create(self, p: Path, data: bytes) -> Path:
        """Write a new file, never replacing one (even one that appeared a moment ago)."""
        p.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(20):
            target = self._free(p)
            try:
                with open(target, "xb") as f:
                    f.write(data)
                return target
            except FileExistsError:
                continue
        raise PermissionDenied(f"could not find a free name for {p}")

    def write_text(self, path: str | Path, text: str, encoding: str = "utf-8") -> str:
        """A new text file, written as given (no line-ending changes). Returns where it went."""
        return self.write_bytes(path, text.encode(encoding))

    def write_bytes(self, path: str | Path, data: bytes) -> str:
        """A new file (never replacing one: "name (1).ext"). Returns where it went."""
        p = self._check(path, True)
        if self.dry_run:
            target = self._free(p)
        else:
            target = self._create(p, data)
        self._trace("file.written", {"path": str(target), "size": len(data), "dry_run": self.dry_run})
        return str(target)

    def append_text(self, path: str | Path, text: str, encoding: str = "utf-8") -> str:
        """Add text to the end of a file (created if missing): what was there is never changed. For logs and notes."""
        p = self._check(path, True)
        if not self.dry_run:
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding=encoding, newline="") as f:
                f.write(text)
        self._trace("file.appended", {"path": str(p), "size": len(text), "dry_run": self.dry_run})
        return str(p)

    def move(self, src: str | Path, dst: str | Path) -> str:
        """Move (or rename) a file. Both ends must be writable; an existing target is never replaced."""
        s = self._check(src, True)
        d = self._check(dst, True)
        if d.is_dir():
            d = d / s.name
        target = self._free(d)
        if not self.dry_run:
            if not s.exists():
                raise FileNotFoundError(str(s))
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():  # appeared since _free: pick again
                target = self._free(d)
            shutil.move(str(s), str(target))
        else:
            self._gone.add(s)
        info: dict = {"from": str(s), "to": str(target), "dry_run": self.dry_run}
        if not self.dry_run:
            try:  # so a later look can find the file again if you rename or move it (the guidance loop)
                st = target.stat()
                info.update(size=st.st_size, mtime=round(st.st_mtime, 3))
            except OSError:
                pass
        self._trace("file.moved", info)
        return str(target)

    def remove_empty_dir(self, path: str | Path) -> bool:
        """Remove a folder only if it is empty (nothing is lost, so write permission is enough)."""
        p = self._check(path, True)
        if not p.is_dir() or any(c not in self._gone for c in p.iterdir()):  # dry-run: as if the moves happened
            return False
        if not self.dry_run:
            p.rmdir()
        self._trace("file.removed_dir", {"path": str(p), "dry_run": self.dry_run})
        return True

    def recycle(self, path: str | Path) -> None:
        """Send a file to the Recycle Bin: restorable, never a permanent delete."""
        if self.delete != "recycle_bin":
            raise PermissionDenied(f"{self.plugin} may not delete files (permissions.files.delete is none)")
        p = self._check(path, True)
        if not self.dry_run:
            try:
                from send2trash import send2trash  # type: ignore[import-not-found]

                send2trash(str(p))
            except ImportError:  # no Recycle Bin support installed: a dated folder in the home directory instead
                bin_ = Path.home() / ".argus-trash" / time.strftime("%Y-%m-%d")
                bin_.mkdir(parents=True, exist_ok=True)
                dst = bin_ / p.name
                i = 1
                while dst.exists():
                    dst, i = bin_ / f"{p.stem} ({i}){p.suffix}", i + 1
                shutil.move(str(p), str(dst))
        self._trace("file.recycled", {"path": str(p), "dry_run": self.dry_run})


def public_host(host: str) -> bool:
    """True when every address the name resolves to is on the public internet (not this machine, the LAN, the
    tailnet or link-local): what "*" in permissions.network allows."""
    try:
        infos = socket.getaddrinfo(host, None)
    except (OSError, UnicodeError):
        return False
    addrs = {i[4][0].split("%")[0] for i in infos}
    return bool(addrs) and all(public_address(a) for a in addrs)


def _checked_connections(allowed):
    """urllib handlers whose connections check the address they actually reached (so a name that resolves to a
    public address for the check and to the LAN for the connect, DNS rebinding, is still refused)."""
    import http.client

    def verify(conn):
        peer = conn.sock.getpeername()[0].split("%")[0]
        if not allowed(conn.host, peer):
            conn.close()
            raise PermissionDenied(f"{conn.host} led to {peer}, which is not a public address")

    class Conn(http.client.HTTPConnection):
        def connect(self):
            super().connect()
            verify(self)

    class SConn(http.client.HTTPSConnection):
        def connect(self):
            super().connect()
            verify(self)

    class H(urllib.request.HTTPHandler):
        def http_open(self, req):
            return self.do_open(Conn, req)

    class HS(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(SConn, req, context=self._context)

    return H(), HS()


def public_address(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_global
    except ValueError:
        return False


class Http:
    """ctx.http: only the hosts in permissions.network. "*" there means any public website (never this machine,
    the LAN or the tailnet, also after a redirect), for plugins like watchers that visit pages you name."""

    def __init__(self, plugin: str, hosts: list[str], trace, timeout: float = 15,
                 is_public: Any = None):
        self.plugin = plugin
        self.hosts = {h.lower() for h in hosts}
        self.any_public = "*" in self.hosts
        self._strict = False  # during a public_only request: the listed hosts don't count
        self._trace = trace
        self.timeout = timeout
        self._public = is_public or (lambda host: public_host(host))  # looked up at call time (tests swap it)
        http = self

        class Redirects(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                http._check(newurl)  # a redirect must be allowed too
                return super().redirect_request(req, fp, code, msg, headers, newurl)

        handlers: list[Any] = [Redirects]
        if self.any_public:
            def allowed(host: str, peer: str) -> bool:
                return (host.lower() in self.hosts and not self._strict) or public_address(peer)

            handlers += _checked_connections(allowed)
        self._opener = urllib.request.build_opener(*handlers)

    def _check(self, url: str) -> str:
        u = urllib.parse.urlparse(url)
        host = (u.hostname or "").lower()
        if u.scheme in ("http", "https") and host:
            if host in self.hosts and not self._strict:
                return url
            if self.any_public and self._public(host):
                return url
        if self.any_public:
            raise PermissionDenied(f"{self.plugin} may only visit public websites, not {host or url!r}")
        raise PermissionDenied(f"{self.plugin} may not call {u.hostname!r} (add it under permissions.network)")

    def request(self, method: str, url: str, *, json_body: Any = None, headers: dict | None = None,
                max_bytes: int | None = None, public_only: bool = False, data: bytes | None = None,
                content_type: str | None = None) -> tuple[int, bytes]:
        """public_only: a page someone else chose (a search result): public websites only, even if this plugin
        may also call a local service (e.g. its search engine), also after redirects."""
        if public_only and not self.any_public:
            raise PermissionDenied(f"{self.plugin} may not visit websites (permissions.network has no \"*\")")
        self._strict = public_only
        try:
            return self._request(method, url, json_body, headers, max_bytes, data, content_type)
        finally:
            self._strict = False

    def _request(self, method: str, url: str, json_body: Any, headers: dict | None,
                 max_bytes: int | None, raw: bytes | None = None, content_type: str | None = None
                 ) -> tuple[int, bytes]:
        self._check(url)
        data = raw if raw is not None else json.dumps(json_body).encode() if json_body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
        if data is not None:
            req.add_header("Content-Type", content_type or "application/json")
        self._trace("http.request", {"method": method, "host": urllib.parse.urlparse(url).hostname})
        try:
            with self._opener.open(req, timeout=self.timeout) as r:
                return r.status, r.read(max_bytes) if max_bytes else r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read(max_bytes) if max_bytes else e.read()

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
        if not path.exists():  # argusd runs elsewhere (the laptop): use this machine's copy of the repo
            here = Path(os.environ.get("ARGUS_PLUGINS_DIR", "plugins")).resolve() / pid / "plugin.py"
            if not here.exists():
                errors[pid] = f"{path} not found on this machine (nor {here})"
                continue
            path = here
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
