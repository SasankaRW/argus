"""argus-worker: run a worker next to (or far from) argusd.

    argus-worker                                  demo plugin, http://127.0.0.1:8600
    argus-worker --url http://laptop:8600 --plugin myplugins.files --cap fs
    argus-worker --once                           run one job (if any) and exit

The token comes from --token, $ARGUS_WORKER_TOKEN, or ARGUS_WORKER_TOKEN in ./.env.
Exit codes: 0 ok, 2 bad arguments or plugin import failed, 4 argus unreachable.
"""

from __future__ import annotations

import argparse
import importlib
import logging
import os
import signal
import sys
from pathlib import Path

from .. import __version__
from ..config import parse_env_file
from ..logs import setup_logging
from .client import ArgusClient, Unreachable
from .runner import Worker

log = logging.getLogger("argus.worker")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="argus-worker", description="Argus worker")
    p.add_argument("--url", default=os.environ.get("ARGUS_URL", "http://127.0.0.1:8600"))
    p.add_argument("--token", default=None)
    p.add_argument("--id", default=os.environ.get("ARGUS_WORKER_ID"), help="worker id (default: worker-<host>)")
    p.add_argument("--plugin", action="append", default=[], help="python module with workflows (repeatable)")
    p.add_argument("--cap", action="append", default=[], help="extra capability, e.g. gpu (repeatable)")
    p.add_argument("--no-demo", action="store_true", help="do not load the built-in demo plugin")
    p.add_argument("--once", action="store_true", help="run at most one job, then exit")
    p.add_argument("--log-level", default="INFO")
    p.add_argument("--version", action="version", version=f"argus-worker {__version__}")
    args = p.parse_args(argv)

    setup_logging(args.log_level, None)
    token = args.token or os.environ.get("ARGUS_WORKER_TOKEN") or parse_env_file(Path(".env")).get(
        "ARGUS_WORKER_TOKEN")

    modules = ([] if args.no_demo else ["argus.worker.demo"]) + args.plugin
    for name in modules:
        try:
            importlib.import_module(name)
        except Exception as e:
            print(f"Could not load plugin {name}: {e}", file=sys.stderr)
            return 2

    worker = Worker(ArgusClient(args.url, token), args.id, capabilities=args.cap)

    presses = 0

    def on_signal(signum, frame):
        nonlocal presses
        presses += 1
        if presses > 1 or not worker.busy:
            raise KeyboardInterrupt
        log.info("stopping after the current job (press Ctrl+C again to stop now)")
        worker.stopping.set()

    signal.signal(signal.SIGINT, on_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, on_signal)

    try:
        if args.once:
            worker.register()
            worker.run_once(wait=0)
        else:
            worker.run_forever()
    except Unreachable as e:
        print(f"Argus is not reachable at {args.url}: {e}", file=sys.stderr)
        return 4
    except KeyboardInterrupt:
        log.info("worker stopped" if not worker.busy else "stopped now; the running job will be retried")
        return 0
    log.info("worker stopped", extra={"jobs": worker.jobs_done})
    return 0


if __name__ == "__main__":
    sys.exit(main())
