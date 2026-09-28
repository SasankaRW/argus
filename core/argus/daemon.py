"""argusd: the Argus daemon.

    argusd                       run with ./argus.yaml (or $ARGUS_CONFIG)
    argusd --config path.yaml    run with a specific config
    argusd --check               validate config and database, then exit

Exit codes: 0 ok, 2 bad config, 3 database problem.
"""

from __future__ import annotations

import argparse
import logging
import sys

from . import __version__
from .config import ConfigError, load_config
from .context import Argus
from .db import StoreError
from .logs import setup_logging

log = logging.getLogger("argus")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="argusd", description="Argus daemon")
    parser.add_argument("--config", help="path to argus.yaml (default: ./argus.yaml or $ARGUS_CONFIG)")
    parser.add_argument("--check", action="store_true", help="validate config and database, then exit")
    parser.add_argument("--version", action="version", version=f"argusd {__version__}")
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(str(e), file=sys.stderr)
        return 2

    setup_logging(cfg.logging.level, cfg.log_path)
    argus = Argus(cfg)
    try:
        argus.open()
    except StoreError as e:
        log.error("database problem", extra={"error": str(e)})
        print(f"Database problem: {e}", file=sys.stderr)
        return 3

    if args.check:
        argus.store.close()
        print(f"OK: config valid, database at schema v{argus.store.schema_version}, argus {__version__}")
        return 0

    import uvicorn  # imported late so --check stays fast

    from .api import create_app

    app = create_app(argus)
    server = uvicorn.Server(
        uvicorn.Config(app, host=cfg.server.host, port=cfg.server.port, log_config=None, access_log=False,
                       ws="websockets-sansio", timeout_graceful_shutdown=5)
    )
    log.info("listening", extra={"host": cfg.server.host, "port": cfg.server.port})
    server.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
