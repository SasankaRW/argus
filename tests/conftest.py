from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from argus.config import JobsConfig
from argus.db import Store
from argus.jobs import JobStore


class FakeClock:
    def __init__(self, start: float = 1_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "argus.db"


@pytest.fixture
def store(db_path: Path):
    s = Store(db_path).open()
    yield s
    s.close()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def jobs(store: Store, clock: FakeClock) -> JobStore:
    cfg = JobsConfig(lease_seconds=60, max_attempts=3, backoff_seconds=[10, 60, 600], plugin_queue_limit=100,
                     plugin_concurrency=50)
    return JobStore(store, cfg, clock=clock)


@pytest.fixture(autouse=True)
def _reset_logging():
    """argusd's setup_logging swaps root handlers; put pytest's back after each test."""
    root = logging.getLogger()
    before = list(root.handlers)
    level = root.level
    yield
    for h in list(root.handlers):
        if h not in before:
            root.removeHandler(h)
            h.close()
    for h in before:
        if h not in root.handlers:
            root.addHandler(h)
    root.setLevel(level)
