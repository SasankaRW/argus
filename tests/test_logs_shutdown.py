"""Requests cancelled when argusd shuts down are not logged as errors; real errors still are."""

from __future__ import annotations

import asyncio
import logging
import sys

from argus.logs import setup_logging, shutdown_cancel


def rec(exc: BaseException, msg: str = "Exception in ASGI application\n") -> logging.LogRecord:
    try:
        raise exc
    except BaseException:
        info = sys.exc_info()
    return logging.LogRecord("uvicorn.error", logging.ERROR, __file__, 1, msg, (), info)


def test_shutdown_cancel_is_dropped_real_errors_kept():
    assert not shutdown_cancel(rec(asyncio.CancelledError("Task cancelled, timeout graceful shutdown exceeded")))
    assert shutdown_cancel(rec(ValueError("boom")))
    assert shutdown_cancel(rec(asyncio.CancelledError(), msg="something else"))
    assert shutdown_cancel(logging.LogRecord("uvicorn.error", logging.WARNING, __file__, 1, "Cancel 3 running task(s)",
                                             (), None))


def test_setup_installs_the_filter_once(tmp_path):
    setup_logging(file=tmp_path / "a.log")
    setup_logging(file=tmp_path / "a.log")
    assert logging.getLogger("uvicorn.error").filters.count(shutdown_cancel) == 1
