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
from .agent import SendAgent
from .notify import toast


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
    toast("Argus couldn't send that", text.splitlines()[0][:200])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="argus", description="Argus command line")
    ap.add_argument("--config", help="argus.yaml (default: $ARGUS_CONFIG or ./argus.yaml)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("send", help="send files or folders to one of my devices")
    s.add_argument("device", help="device id, e.g. calypso")
    s.add_argument("paths", nargs="+")
    sub.add_parser("send-agent", help="run the Send agent (receiver, queue, discovery)")
    args = ap.parse_args(argv)
    try:
        cfg = load_config(config_path(args.config))
    except ConfigError as e:
        if args.cmd == "send":
            _problem(str(e))
        else:
            print(e, file=sys.stderr)
        return 2
    if not cfg.send.device_id:
        msg = "send.device_id is not set in the config (see the send: block in argus.example.yaml)"
        if args.cmd == "send":
            _problem(msg)
        else:
            print(msg, file=sys.stderr)
        return 2
    agent = SendAgent(cfg.send, argus_token=cfg.secrets.worker_token)
    if args.cmd == "send":
        try:
            oid = agent.enqueue(args.device, args.paths)
        except ValueError as e:
            _problem(f"argus send: {e}")
            return 2
        name = agent.devices.name_of(args.device)
        print(f"queued {oid[:8]}: the agent sends it as soon as {name} is reachable")
        what = Path(args.paths[0]).name if len(args.paths) == 1 else f"{len(args.paths)} items"
        toast(f"Sending {what} to {name}", "You'll get another notification when it arrives")
        return 0
    agent.notify = True
    fmt = "%(asctime)s %(name)s %(message)s"
    if sys.stdout is None or sys.stderr is None:  # pythonw (the logon task on Windows) has no console
        agent.data.mkdir(parents=True, exist_ok=True)
        log = open(agent.data / "agent.log", "a", encoding="utf-8", buffering=1)  # noqa: SIM115 (open for the process's life)
        sys.stdout = sys.stdout or log
        sys.stderr = sys.stderr or log
        logging.basicConfig(level=logging.INFO, format=fmt, stream=log)
    else:
        logging.basicConfig(level=logging.INFO, format=fmt)
    try:
        asyncio.run(agent.run_forever())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
