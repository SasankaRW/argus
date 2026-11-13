"""argusd: the Argus daemon.

    argusd                       run with ./argus.yaml (or $ARGUS_CONFIG)
    argusd --config path.yaml    run with a specific config
    argusd --check               validate config and database, then exit
    argusd --restore latest      put the newest backup back (argusd stopped; or --restore FILE). Without --yes
                                 it only says what it would do. docs/restore.md

Exit codes: 0 ok, 2 bad config, 3 database problem, 4 restore refused.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

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
    parser.add_argument("--restore", metavar="FILE|latest",
                        help="put a backup back as the database (stop argusd first); see docs/restore.md")
    parser.add_argument("--yes", action="store_true", help="with --restore: do it (otherwise only show the plan)")
    parser.add_argument("--version", action="version", version=f"argusd {__version__}")
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(str(e), file=sys.stderr)
        return 2

    setup_logging(cfg.logging.level, cfg.log_path)
    if args.restore:
        return restore_cmd(cfg, args.restore, args.yes)
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
        from .plugins import discover, placement_warnings

        for w in placement_warnings(cfg, discover(cfg)[0]):
            print(f"warning: {w}")
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


def restore_cmd(cfg, which: str, yes: bool) -> int:
    """`argusd --restore FILE|latest [--yes]`: checks first, says what it will do, and does it only with --yes."""
    from .backup import Backups, RestoreError, argusd_listening, restore
    from .db.store import available_migrations

    backups = Backups(cfg.db_path, cfg.backup.keep)
    if which == "latest":
        found = backups.latest()
        if found is None:
            print(f"No backups in {backups.dir}.", file=sys.stderr)
            return 4
    else:
        found = Path(which)
        if not found.is_absolute() and not found.exists():
            found = backups.dir / which  # a name from `GET /backups` or Helios
    check = Backups.verify(found) if found.is_file() else {"ok": False, "problem": "no such file"}
    migrations = available_migrations()
    code_schema = migrations[-1][0] if migrations else 0
    print(f"Backup:   {found}")
    print(f"          {'ok' if check['ok'] else 'FAILED: ' + str(check.get('problem'))}"
          + (f", schema v{check['schema']}, {check['jobs']} jobs" if check["ok"] else ""))
    print(f"Database: {cfg.db_path}" + ("" if cfg.db_path.exists() else " (none yet)"))
    print(f"          the current one is moved to {cfg.db_path.parent / 'replaced'}, not deleted")
    if argusd_listening(cfg.server.host, cfg.server.port):
        print(f"argusd is running on port {cfg.server.port}. Stop it first (on the laptop: "
              "sudo systemctl stop argusd), then run this again.", file=sys.stderr)
        return 4
    if not yes:
        print("Nothing changed. Run it again with --yes to restore.")
        return 0 if check["ok"] else 4
    try:
        r = restore(found, cfg.db_path, code_schema)
    except RestoreError as e:
        print(f"Restore refused: {e}", file=sys.stderr)
        return 4
    print(f"Restored {r['file']}: schema v{r['schema']}, {r['jobs']} jobs."
          + (f" The old database is in {r['replaced']}." if r["replaced"] else ""))
    print("Start argusd again; `argusd --check` first if you like.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
