"""Argus: local-first orchestrator for LLM workflows."""

from __future__ import annotations

from importlib import metadata
from pathlib import Path


def _read_version() -> str:
    # Prefer the VERSION file in a source checkout, fall back to installed metadata.
    version_file = Path(__file__).resolve().parents[2] / "VERSION"
    if version_file.exists():
        return version_file.read_text(encoding="utf-8").strip()
    try:
        return metadata.version("argus")
    except metadata.PackageNotFoundError:
        return "0.0.0"


__version__ = _read_version()
