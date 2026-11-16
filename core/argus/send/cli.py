"""argus send <device> <path...>   and   argus send-agent

`send` puts the share in the agent's disk queue (send-queue.db) and returns; a running agent picks it up within
seconds. `send-agent` runs the agent: the receiver on the configured port, the queue, LAN discovery.
Settings come from the `send:` block of argus.yaml (ARGUS_CONFIG or --config).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from ..config import ConfigError, load_config
from .agent import SendAgent


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
        cfg = load_config(args.config)
    except ConfigError as e:
        print(e, file=sys.stderr)
        return 2
    if not cfg.send.device_id:
        print("send.device_id is not set in the config (see the send: block in argus.example.yaml)", file=sys.stderr)
        return 2
    agent = SendAgent(cfg.send, argus_token=cfg.secrets.worker_token)
    if args.cmd == "send":
        try:
            oid = agent.enqueue(args.device, args.paths)
        except ValueError as e:
            print(f"argus send: {e}", file=sys.stderr)
            return 2
        print(f"queued {oid[:8]}: the agent sends it as soon as {agent.devices.name_of(args.device)} is reachable")
        return 0
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    try:
        asyncio.run(agent.run_forever())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
