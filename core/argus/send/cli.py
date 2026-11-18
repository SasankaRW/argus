"""argus send <device> <path...>   and   argus send-agent

`send` puts the share in the agent's disk queue (send-queue.db) and returns; a running agent picks it up within
seconds. `send-agent` runs the agent: the receiver on the configured port, the queue, LAN discovery.
Settings come from the `send:` block of argus.yaml (ARGUS_CONFIG or --config).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from pathlib import Path

from ..config import ConfigError, load_config
from . import clip as clipmod
from . import reload, toast
from .agent import SendAgent
from .sender import label_of


def config_path(given: str | None) -> str | None:
    """--config, else $ARGUS_CONFIG, else ./argus.yaml, else the argus.yaml of the checkout this command runs from.

    The right-click menu starts `argus send` from wherever Explorer is (often System32), where ./argus.yaml is not."""
    if given or os.environ.get("ARGUS_CONFIG") or Path("argus.yaml").exists():
        return given
    here = Path(__file__).resolve()
    for root in (here.parents[3], Path(sys.executable).resolve().parents[2]):  # repo/core/argus/send, .venv/Scripts
        if (root / "argus.yaml").is_file():
            return str(root / "argus.yaml")
    return given


def _problem(text: str) -> None:
    """A failed `argus send`: said on stderr, kept in a log and shown as a notification (the menu has no window)."""
    print(text, file=sys.stderr)
    try:
        d = Path.home() / ".argus-send"
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "send-cli.log", "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")
    except OSError:
        pass
    toast.Toaster().problem("Argus couldn't send that", text.splitlines()[0][:200])


def local_call(cfg, method: str, path: str, body: dict | None = None, opener=None) -> tuple[int, dict]:
    """A call to this PC's own Send agent (token only): (status, json)."""
    import json
    import urllib.error
    import urllib.request

    headers = {"Content-Type": "application/json"}
    if cfg.secrets.worker_token:
        headers["Authorization"] = f"Bearer {cfg.secrets.worker_token}"
    req = urllib.request.Request(f"http://127.0.0.1:{cfg.send.port}{path}", method=method, headers=headers,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with (opener or urllib.request.urlopen)(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


def copy_url_command(url: str) -> int:
    """argus-send://copy/<url> from a link notification's Copy button: the link goes on the clipboard."""
    import base64

    from .clipwin import WinClipboard

    raw = url.rstrip("/").rsplit("/", 1)[-1].replace("-", "+").replace("_", "/")
    try:
        text = base64.b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8")
    except ValueError:
        return 1
    if not clipmod.as_url(text):
        return 1
    try:
        WinClipboard().write(text, exclude=False)
    except OSError:
        return 1
    return 0


def link_command(cfg, url: str) -> int:
    """argus send --link <url>: the PC's agent sends it to the phone as a notification."""
    if not clipmod.as_url(url):
        _problem("argus send --link: that is not a web link (http:// or https://)")
        return 2
    try:
        status, body = local_call(cfg, "POST", "/send/clip/link", {"url": url})
    except OSError as e:
        _problem(f"argus send --link: the Send agent is not running ({e})")
        return 1
    if status != 200:
        _problem(f"argus send --link: {body.get('detail', status)}")
        return 1
    print("sent the link to the phone" if body.get("via") == "direct" else "queued the link for the phone")
    return 0


def token_command(cfg, args, opener=None) -> int:
    """argus token new|list|revoke: calls the server's /tokens routes with argus.yaml's URL and worker token."""
    import json
    import urllib.error
    import urllib.parse
    import urllib.request

    base = (cfg.send.argus_url or f"http://{cfg.server.host}:{cfg.server.port}").rstrip("/")
    headers = {"Content-Type": "application/json"}
    if cfg.secrets.worker_token:
        headers["Authorization"] = f"Bearer {cfg.secrets.worker_token}"
    if args.token_cmd == "new":
        method, path, data = "POST", "/tokens", json.dumps({"name": args.name}).encode()
    elif args.token_cmd == "revoke":
        method, path, data = "DELETE", "/tokens/" + urllib.parse.quote(args.name, safe=""), None
    else:
        method, path, data = "GET", "/tokens", None
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with (opener or urllib.request.urlopen)(req, timeout=15) as r:
            body = json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        print(f"argus token: {e.code} {e.read().decode('utf-8', 'replace')[:200]}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"argus token: cannot reach {base}: {e}", file=sys.stderr)
        return 1
    if args.token_cmd == "new":
        print(f"{body['name']}: {body['token']}\nThis is shown once; store it now.")
    elif args.token_cmd == "revoke":
        print(f"revoked {args.name}")
    else:
        for t in body:
            state = "revoked" if t["revoked_at"] else "active"
            used = time.strftime("%Y-%m-%d %H:%M", time.localtime(t["last_used_at"])) if t["last_used_at"] else "never"
            print(t["name"], state, f"last used {used}", sep="\t")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="argus", description="Argus command line")
    ap.add_argument("--config", help="argus.yaml (default: $ARGUS_CONFIG or ./argus.yaml)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("send", help="send files or folders to one of my devices")
    s.add_argument("device", nargs="?", help="device id, e.g. calypso")
    s.add_argument("paths", nargs="*")
    s.add_argument("--link", metavar="URL", help="send a web link to the phone as a notification, not a file")
    sub.add_parser("send-agent", help="run the Send agent (receiver, queue, discovery)")
    cu = sub.add_parser("cancel-url", help="cancel a share from its notification (argus-send://cancel/<id>)")
    cu.add_argument("url")
    tk = sub.add_parser("token", help="named API tokens: new <name>, list, revoke <name>")
    tsub = tk.add_subparsers(dest="token_cmd", required=True)
    tsub.add_parser("new", help="make a token (shown once)").add_argument("name")
    tsub.add_parser("list", help="list the tokens")
    tsub.add_parser("revoke", help="revoke a token").add_argument("name")
    args = ap.parse_args(argv)
    try:
        cfg = load_config(config_path(args.config))
    except ConfigError as e:
        if args.cmd == "send":
            _problem(str(e))
        else:
            print(e, file=sys.stderr)
        return 2
    if args.cmd == "token":
        return token_command(cfg, args)
    if not cfg.send.device_id:
        msg = "send.device_id is not set in the config (see the send: block in argus.example.yaml)"
        if args.cmd == "send":
            _problem(msg)
        else:
            print(msg, file=sys.stderr)
        return 2
    if args.cmd == "send" and args.link:
        return link_command(cfg, args.link)
    if args.cmd == "send" and not (args.device and args.paths):
        ap.error("send needs a device and paths, or --link URL")
    if args.cmd == "cancel-url" and args.url.startswith("argus-send://copy/"):
        return copy_url_command(args.url)
    agent = SendAgent(cfg.send, argus_token=cfg.secrets.worker_token)
    if args.cmd == "cancel-url" or (args.cmd == "send" and args.device == "cancel"):  # argus send cancel <id>
        oid = args.url.rstrip("/").rsplit("/", 1)[-1] if args.cmd == "cancel-url" else args.paths[0]
        ok = agent.cancel(oid)
        print(f"cancelled {oid[:8]}" if ok else f"nothing to cancel for {oid[:8]}")
        return 0 if ok else 1
    if args.cmd == "send":
        try:
            oid = agent.enqueue(args.device, args.paths)
        except ValueError as e:
            _problem(f"argus send: {e}")
            return 2
        print(f"queued {oid[:8]}: the agent sends it as soon as {agent.devices.name_of(args.device)} is reachable")
        return 0  # the agent's notification appears as soon as it starts moving
    if toast.enabled():  # the "Argus" notification with a live progress bar (Windows)
        toast.register_app()
        toast.register_cancel_protocol()
        shower = toast.Toaster(cfg.send.device_id, agent.devices.name_of)
        agent.on_progress = shower.show
        agent.on_received = lambda offer, saved: shower.received(
            offer["offer_id"], agent.devices.name_of(offer.get("origin") or offer["from"]), label_of(offer["files"]),
            saved)
        agent.clip.on_link = lambda url, origin: shower.link(
            "clip-" + str(int(time.time() * 1000)), agent.devices.name_of(origin), url)
    if sys.platform == "win32":
        from .clipwin import WinClipboard

        agent.clip.attach(WinClipboard())  # the listener thread starts with the agent, and only while sync is on
    fmt = "%(asctime)s %(name)s %(message)s"
    if sys.stdout is None or sys.stderr is None:  # pythonw (the logon task on Windows) has no console
        agent.data.mkdir(parents=True, exist_ok=True)
        log = open(agent.data / "agent.log", "a", encoding="utf-8", buffering=1)  # noqa: SIM115 (open for the process's life)
        sys.stdout = sys.stdout or log
        sys.stderr = sys.stderr or log
        logging.basicConfig(level=logging.INFO, format=fmt, stream=log)
    else:
        logging.basicConfig(level=logging.INFO, format=fmt)
    agent.reload_on_new_code = True
    try:
        asyncio.run(agent.run_forever())
    except KeyboardInterrupt:
        pass
    if agent.restart_requested and not reload.under_systemd():
        reload.respawn()  # the port is free again (run_forever stopped the receiver): the copy binds it
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
