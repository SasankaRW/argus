"""Configuration: one argus.yaml plus a .env file for secrets, validated at startup.

A bad config stops argusd with a clear message instead of failing later.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class ConfigError(Exception):
    """Raised when the configuration cannot be loaded. The message is meant for a human."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InstanceConfig(_Strict):
    name: str = "argus"
    host: Literal["pc", "laptop"] = "pc"


class ServerConfig(_Strict):
    host: str = "127.0.0.1"
    port: int = Field(8600, ge=1, le=65535)


class DatabaseConfig(_Strict):
    path: Path = Path("data/argus.db")


class LoggingConfig(_Strict):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    file: Path | None = Path("logs/argus.log")


class OllamaConfig(_Strict):
    url: str = "http://127.0.0.1:11434"
    timeout_seconds: float = Field(120, gt=0, le=3600)
    keep_alive: str = "10m"  # how long Ollama keeps the last model loaded


class TierConfig(_Strict):
    provider: Literal["ollama", "claude"]
    model: str | None = None  # Ollama model name; for Claude an optional --model alias
    label: str | None = None  # shown on the map
    group: str | None = None  # map group, e.g. pc or cloud


def _default_tiers() -> dict[str, TierConfig]:
    return {
        "T1": TierConfig(provider="ollama", model="qwen2.5-coder:7b", group="pc"),
        "T2": TierConfig(provider="ollama", model="qwen2.5-coder:14b", group="pc"),
        "T3": TierConfig(provider="claude", group="cloud"),
    }


class ModelsConfig(_Strict):
    tiers: dict[str, TierConfig] = Field(default_factory=_default_tiers)
    chain: list[str] = Field(default_factory=lambda: ["T1", "T2", "T3"])  # default escalation order
    attempts_per_tier: int = Field(2, ge=1, le=5)  # tries (with feedback) before moving up a tier
    breaker_failures: int = Field(3, ge=1, le=100)  # consecutive failures that open the circuit breaker
    breaker_open_seconds: float = Field(60, gt=0, le=3600)

    @field_validator("tiers")
    @classmethod
    def _tier_names(cls, v: dict[str, TierConfig]) -> dict[str, TierConfig]:
        for name in v:
            if not name.replace("_", "").isalnum() or len(name) > 20:
                raise ValueError(f"tier name {name!r} must be short letters/digits, like T1")
        return v

    def model_post_init(self, _ctx) -> None:
        unknown = [t for t in self.chain if t not in self.tiers]
        if unknown:
            raise ValueError(f"chain names unknown tiers: {', '.join(unknown)}")


class ClaudeConfig(_Strict):
    # The claude CLI, called in print mode with every tool removed: text in, text out, nothing else.
    command: list[str] = Field(default_factory=lambda: ["claude"])
    args: list[str] = Field(default_factory=lambda: [
        "-p", "--output-format", "json", "--max-turns", "1", "--disallowedTools", "*",
        "--no-session-persistence",
    ])
    timeout_seconds: float = Field(300, gt=0, le=3600)
    calls_per_day: int = Field(30, ge=0, le=10000)


class JobsConfig(_Strict):
    lease_seconds: int = Field(60, ge=5, le=3600)
    heartbeat_seconds: int = Field(15, ge=1, le=600)
    max_attempts: int = Field(3, ge=1, le=20)
    backoff_seconds: list[int] = Field(default_factory=lambda: [10, 60, 600])
    watchdog_interval_seconds: float = Field(5, gt=0, le=300)
    plugin_queue_limit: int = Field(100, ge=1)

    @field_validator("backoff_seconds")
    @classmethod
    def _backoff_not_empty(cls, v: list[int]) -> list[int]:
        if not v or any(x < 0 for x in v):
            raise ValueError("must be a non-empty list of seconds (0 or more)")
        return v


class EventsConfig(_Strict):
    retention_days: int = Field(90, ge=1, le=3650)
    stream_queue: int = Field(200, ge=10, le=10000)  # batches a slow viewer may lag before it is dropped


class NtfyConfig(_Strict):
    # Phone notifications. The topic is a secret (NTFY_TOPIC in .env): anyone who knows it can read it.
    url: str = "https://ntfy.sh"          # or your own ntfy server
    timeout_seconds: float = Field(10, gt=0, le=120)
    max_attempts: int = Field(8, ge=1, le=50)  # then the message is marked failed (and shown in Helios)
    reply_retry_seconds: float = Field(5, gt=0, le=300)  # wait before reconnecting to the reply topic


class ApprovalsConfig(_Strict):
    # The address your phone uses to reach Argus (Tailscale later, e.g. http://laptop:8600). Without it the
    # notification has no Approve/Reject buttons and you decide in Helios instead.
    public_url: str | None = None
    remind_hours: float = Field(24, gt=0, le=24 * 30)   # one reminder if nobody decided by then
    expire_hours: float = Field(168, gt=0, le=24 * 90)  # then the approval counts as "no" and the job goes on

    @field_validator("public_url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        if v is None or v.strip() == "":
            return None
        if not v.startswith(("http://", "https://")):
            raise ValueError("must start with http:// or https://")
        return v.rstrip("/")


class PowerConfig(_Strict):
    mode: Literal["simulated", "real"] = "simulated"


class PathsConfig(_Strict):
    allowed: list[Path] = Field(default_factory=list)
    blocked: list[Path] = Field(default_factory=list)


class Secrets(BaseModel):
    """Values from .env and the environment. Never logged."""

    model_config = ConfigDict(extra="ignore")
    admin_password: str | None = None
    worker_token: str | None = None
    ntfy_topic: str | None = None
    ntfy_token: str | None = None  # only for a private ntfy server with access control
    ntfy_reply_topic: str | None = None  # optional; derived from the topic when unset


class Config(_Strict):
    instance: InstanceConfig = Field(default_factory=InstanceConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    ollama: OllamaConfig = Field(default_factory=OllamaConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    claude: ClaudeConfig = Field(default_factory=ClaudeConfig)
    jobs: JobsConfig = Field(default_factory=JobsConfig)
    events: EventsConfig = Field(default_factory=EventsConfig)
    ntfy: NtfyConfig = Field(default_factory=NtfyConfig)
    approvals: ApprovalsConfig = Field(default_factory=ApprovalsConfig)
    power: PowerConfig = Field(default_factory=PowerConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)

    # Filled in by load_config, not read from YAML.
    base_dir: Path = Field(default=Path("."), exclude=True)
    secrets: Secrets = Field(default_factory=Secrets, exclude=True)

    def resolve(self, p: Path) -> Path:
        """Relative paths in the config are relative to the config file's folder."""
        return p if p.is_absolute() else (self.base_dir / p).resolve()

    @property
    def db_path(self) -> Path:
        return self.resolve(self.database.path)

    @property
    def log_path(self) -> Path | None:
        return self.resolve(self.logging.file) if self.logging.file else None


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigError(f"{path.name} line {lineno}: expected KEY=value, got {raw!r}")
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _format_validation_error(path: Path, err: ValidationError) -> str:
    lines = [f"Config error in {path}:"]
    for e in err.errors():
        where = ".".join(str(p) for p in e["loc"]) or "(top level)"
        msg = e["msg"]
        if e["type"] == "extra_forbidden":
            msg = "unknown setting (check the spelling against argus.example.yaml)"
        lines.append(f"  - {where}: {msg}")
    return "\n".join(lines)


def load_config(path: str | os.PathLike | None = None) -> Config:
    """Load and validate the config. Raises ConfigError with a readable message on any problem."""
    cfg_path = Path(path or os.environ.get("ARGUS_CONFIG", "argus.yaml")).resolve()
    if not cfg_path.exists():
        raise ConfigError(
            f"Config file not found: {cfg_path}\n"
            "  Copy argus.example.yaml to argus.yaml, or set ARGUS_CONFIG to its path."
        )
    try:
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"Config file {cfg_path} is not valid YAML:\n  {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"Config file {cfg_path} must be a mapping of sections, got {type(raw).__name__}")

    try:
        cfg = Config.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(_format_validation_error(cfg_path, e)) from e

    cfg.base_dir = cfg_path.parent
    env = parse_env_file(cfg_path.parent / ".env")
    env.update({k: v for k, v in os.environ.items() if k.startswith(("ARGUS_", "NTFY_"))})
    cfg.secrets = Secrets(
        admin_password=env.get("ARGUS_ADMIN_PASSWORD"),
        worker_token=env.get("ARGUS_WORKER_TOKEN"),
        ntfy_topic=env.get("NTFY_TOPIC") or None,
        ntfy_token=env.get("NTFY_TOKEN") or None,
        ntfy_reply_topic=env.get("NTFY_REPLY_TOPIC") or None,
    )
    return cfg
