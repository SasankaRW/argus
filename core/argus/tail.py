"""argus-events: watch Argus events live in a terminal.

    argus-events                               everything from now on
    argus-events --kinds job.,worker.          only job and worker events
    argus-events --job 01M3...                 one job
    argus-events --since 0                     replay from the beginning, then follow
    argus-events --json                        one JSON object per line (for scripts)

Reconnects by itself and never misses an event (it resumes from the last one it printed).
The token comes from --token, $ARGUS_WORKER_TOKEN, or ARGUS_WORKER_TOKEN in ./.env.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
from pathlib import Path

from .config import parse_env_file

COLORS = {"job.succeeded": 32, "job.dead": 31, "job.retry": 33, "job.waiting": 36, "worker.offline": 31,
          "worker.online": 32, "edge.added": 35, "component.added": 35, "step.failed": 31}


def fmt(e: dict, color: bool) -> str:
    t = time.strftime("%H:%M:%S", time.localtime(e["at"])) + f".{int(e['at'] * 1000) % 1000:03d}"
    route = (e.get("from") or "") + (f" -> {e['to']}" if e.get("to") else "")
    job = e["job_id"][-6:] if e.get("job_id") else ""
    if e.get("step"):
        job += f" {e['step']}"
    data = e.get("data") or {}
    extra = " ".join(f"{k}={v}" for k, v in data.items() if k not in ("job_id",) and v not in (None, "", [], {}))
    kind = e["kind"]
    if color and kind in COLORS:
        kind = f"\x1b[{COLORS[kind]}m{kind:<16}\x1b[0m"
    else:
        kind = f"{kind:<16}"
    return f"{t}  {kind}  {route:<28}  {job:<14}  {extra}".rstrip()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="argus-events", description="Watch Argus events live")
    p.add_argument("--url", default=os.environ.get("ARGUS_URL", "http://127.0.0.1:8600"))
    p.add_argument("--token", default=None)
    p.add_argument("--kinds", default=None, help="comma-separated prefixes, e.g. job.,worker.")
    p.add_argument("--job", default=None)
    p.add_argument("--since", type=int, default=None, help="replay events after this seq first (0 = all)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--count", type=int, default=None, help="exit after this many events")
    args = p.parse_args(argv)

    from websockets.exceptions import ConnectionClosed, InvalidStatus
    from websockets.sync.client import connect

    token = args.token or os.environ.get("ARGUS_WORKER_TOKEN") or parse_env_file(Path(".env")).get(
        "ARGUS_WORKER_TOKEN")
    base = args.url.rstrip("/").replace("http://", "ws://").replace("https://", "wss://")
    color = sys.stdout.isatty() and not args.json and os.environ.get("NO_COLOR") is None
    if color and os.name == "nt":
        os.system("")  # turns on ANSI colours in the Windows console
    since, seen, wait, said_waiting = args.since, 0, 1.0, False

    while True:
        q = {k: v for k, v in {"kinds": args.kinds, "job": args.job, "since": since}.items() if v is not None}
        headers = {"Authorization": f"Bearer {token}"} if token else None
        try:
            with connect(f"{base}/ws/events?{urllib.parse.urlencode(q)}", additional_headers=headers,
                         open_timeout=5) as ws:
                wait, said_waiting = 1.0, False
                for raw in ws:
                    msg = json.loads(raw)
                    if msg["type"] == "hello" and since is None:
                        since = msg["seq"]
                        print(f"# watching {args.url} (argus {msg['version']}); Ctrl+C to stop", file=sys.stderr)
                    elif msg["type"] == "events":
                        for e in msg["events"]:
                            since = e["seq"]
                            print(json.dumps(e) if args.json else fmt(e, color), flush=True)
                            seen += 1
                            if args.count is not None and seen >= args.count:
                                return 0
                    elif msg["type"] == "reset":
                        print(f"# {msg['reason']}", file=sys.stderr)
        except KeyboardInterrupt:
            return 0
        except InvalidStatus as e:
            if e.response.status_code in (401, 403):
                print("Wrong or missing token (set ARGUS_WORKER_TOKEN or pass --token).", file=sys.stderr)
                return 2
            raise
        except (ConnectionClosed, OSError, TimeoutError):
            if not said_waiting:
                print(f"# argus not reachable at {args.url}, retrying...", file=sys.stderr)
                said_waiting = True
        try:
            time.sleep(wait)
        except KeyboardInterrupt:
            return 0
        wait = min(wait * 2, 10.0)


if __name__ == "__main__":
    sys.exit(main())
