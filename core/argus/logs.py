"""JSON-lines logging to stdout and a rotating file."""

from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import re
import sys
import time
from pathlib import Path

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime", "color_message"}


# Tokens travel in some URLs (the WebSocket's ?token=, the phone page's ?t=); uvicorn logs those URLs.
_SECRET_IN_URL = re.compile(r"([?&](?:token|t)=)[^&\s\"']+")
_NAMED_TOKEN = re.compile(r"\bar_[A-Za-z0-9_-]{40,}")  # a named API token (tokens.PREFIX) wherever it appears


def redact(text: str) -> str:
    return _NAMED_TOKEN.sub("***", _SECRET_IN_URL.sub(r"\1***", text))


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                entry[key] = value
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str, ensure_ascii=False)


def shutdown_cancel(record: logging.LogRecord) -> bool:
    """False for uvicorn's "Exception in ASGI application" when the exception is only a request cancelled at
    shutdown (a long-poll still open when argusd restarts): hundreds of these a month buried the real errors."""
    exc = record.exc_info[1] if record.exc_info else None
    return not (isinstance(exc, asyncio.CancelledError) and record.getMessage().startswith("Exception in ASGI"))


def setup_logging(level: str = "INFO", file: Path | None = None) -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(level)
    fmt = JsonFormatter()

    # To the terminal when there is one. Started in the background ("dev.ps1 up") there is only the file, so the
    # log isn't written twice (the redirected output would grow without rotation).
    if sys.stdout is not None and (file is None or sys.stdout.isatty()):
        out = logging.StreamHandler(sys.stdout)
        out.setFormatter(fmt)
        root.addHandler(out)

    if file is not None:
        file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            file, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)

    # Keep uvicorn's own access log quiet; our API logs what matters.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    err = logging.getLogger("uvicorn.error")
    if shutdown_cancel not in err.filters:
        err.addFilter(shutdown_cancel)
