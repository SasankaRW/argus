"""Configuration: one argus.yaml plus a .env file for secrets, validated at startup.

A bad config stops argusd with a clear message instead of failing later.
"""

from __future__ import annotations

import os
import re
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
    # Each plugin's share when its manifest doesn't say (permissions.claude_calls_per_day; 0 there turns it off).
    # Claude is the last try when the local models can't do something; after that the plugin asks you.
    plugin_calls_per_day: int = Field(10, ge=0, le=1000)


# Job priorities (higher runs first): interactive work, then jobs resuming after an approval, then scheduled
# work, then night batches. Within a priority, oldest first.
PRIORITY_INTERACTIVE = 90
PRIORITY_RESUMED = 80
PRIORITY_SCHEDULED = 50
PRIORITY_BATCH = 20


class JobsConfig(_Strict):
    lease_seconds: int = Field(60, ge=5, le=3600)
    heartbeat_seconds: int = Field(15, ge=1, le=600)
    max_attempts: int = Field(3, ge=1, le=20)
    backoff_seconds: list[int] = Field(default_factory=lambda: [10, 60, 600])
    watchdog_interval_seconds: float = Field(5, gt=0, le=300)
    plugin_queue_limit: int = Field(100, ge=1)
    plugin_concurrency: int = Field(1, ge=1, le=64)  # jobs of one plugin running at once (backpressure)
    concurrency: dict[str, int] = Field(default_factory=dict)  # per-plugin overrides, e.g. {demo: 4}

    def limit_for(self, plugin: str) -> int:
        return self.concurrency.get(plugin, self.plugin_concurrency)

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
    # Quiet by default: a plugin's ordinary messages (ctx.notify with priority min/low/default) wait for the evening
    # summary. Approvals, failures, reminders, warnings and high/urgent messages always come at once.
    quiet: bool = True


class ApprovalsConfig(_Strict):
    # How the phone's Approve / Reject buttons reach Argus:
    #   tailscale (default): straight to Argus at public_url, so they only work while the phone is on Tailscale.
    #       When the phone comes back online, Argus pushes whatever is still waiting (see `phone`).
    #   ntfy: through a private reply topic on the ntfy server, so they work anywhere; anyone who knows your
    #       ntfy topic could then approve too. Use only with your own ntfy server behind a login.
    buttons: Literal["tailscale", "ntfy"] = "tailscale"
    public_url: str | None = None  # how the phone reaches Argus, e.g. https://saspc.tail1234.ts.net
    phone: str | None = None  # the phone's Tailscale device name; empty = no "back online" push
    presence_seconds: float = Field(30, ge=5, le=3600)  # how often `tailscale status` is checked
    back_online_cooldown_minutes: float = Field(15, ge=0, le=24 * 60)  # at most one push per this long
    tailscale_command: list[str] = Field(default_factory=lambda: ["tailscale"])
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


def _check_cron(v: str) -> str:
    from .cron import CronError, parse

    try:
        parse(v)
    except CronError as e:
        raise ValueError(str(e)) from None
    return v


class _JobSpec(_Strict):
    """What a schedule or trigger enqueues."""

    plugin: str = Field(min_length=1, max_length=100)
    workflow: str = Field(min_length=1, max_length=100)
    input: dict = Field(default_factory=dict)
    needs: list[str] = Field(default_factory=list)
    priority: int = Field(PRIORITY_SCHEDULED, ge=0, le=100)
    model: str | None = None  # the model the job mostly uses: GPU jobs are grouped by it (fewer model swaps)
    window: str | None = None  # a name from `windows`: the job only starts inside that window


class ScheduleConfig(_JobSpec):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{0,62}$")
    cron: str  # "0 7 * * *", "@daily", "*/15 * * * *" (local time)
    enabled: bool = True

    @field_validator("cron")
    @classmethod
    def _cron_ok(cls, v: str) -> str:
        return _check_cron(v)


class FolderTrigger(_JobSpec):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{0,62}$")
    path: str  # on the worker's machine
    worker: str  # the worker (id or host) that watches it
    patterns: list[str] = Field(default_factory=lambda: ["*"])
    ignore: list[str] = Field(default_factory=lambda: ["*.crdownload", "*.part", "*.tmp", "~$*", ".*"])
    settle_seconds: float = Field(120, ge=0, le=3600)  # unchanged this long before it counts as finished
    recursive: bool = False
    priority: int = Field(PRIORITY_SCHEDULED, ge=0, le=100)


class WebhookTrigger(_JobSpec):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{0,62}$")
    secret_env: str  # the .env variable holding this hook's secret (never the secret itself)
    style: Literal["argus", "github"] = "argus"
    priority: int = Field(PRIORITY_INTERACTIVE, ge=0, le=100)


class TriggersConfig(_Strict):
    folders: list[FolderTrigger] = Field(default_factory=list)
    webhooks: list[WebhookTrigger] = Field(default_factory=list)


class PowerConfig(_Strict):
    # simulated: Argus only logs "would wake" / "would shut down" (the PC while developing). real comes with the
    # laptop deployment (Wake-on-LAN and the desktop runner's shutdown).
    mode: Literal["simulated", "real"] = "simulated"
    idle_minutes: float = Field(20, ge=1, le=24 * 60)  # nothing for the PC to do this long -> shut it down
    pc_needs: list[str] = Field(default_factory=lambda: ["gpu", "desktop"])  # job needs only the PC can serve
    # Buttons in Helios (Power page, phone). Wake-on-LAN is sent by argusd, so it works once argusd runs on the
    # laptop; sleep / shut down / restart run on the PC's worker.
    pc_mac: str | None = None  # the PC's wired network card, e.g. 04:7C:16:AB:CD:EF (ipconfig /all)
    wol_broadcast: str = "255.255.255.255"
    wol_port: int = Field(9, ge=1, le=65535)
    shutdown_delay_seconds: int = Field(60, ge=0, le=3600)  # time to cancel a shutdown or restart
    warn_minutes: float = Field(5, ge=0, le=120)  # real mode: phone warning this long before an automatic shutdown
    shutdown_manual_sessions: bool = False  # real mode: also shut down a PC you switched on yourself

    @field_validator("pc_mac")
    @classmethod
    def _mac(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        h = re.sub(r"[^0-9A-Fa-f]", "", v)
        if len(h) != 12:
            raise ValueError(f"pc_mac {v!r} is not a MAC address like 04:7C:16:AB:CD:EF")
        return ":".join(h[i:i + 2] for i in range(0, 12, 2)).upper()


class PluginsConfig(_Strict):
    dirs: list[Path] = Field(default_factory=lambda: [Path("plugins")])  # folders holding one folder per plugin
    live: list[str] = Field(default_factory=list)  # plugins promoted out of dry-run
    config: dict[str, dict] = Field(default_factory=dict)  # per-plugin settings over the manifest defaults


class ShareConfig(_Strict):
    max_mb: float = Field(50, gt=0, le=2000)  # per share, all files together
    keep_days: float = Field(7, gt=0, le=365)  # shared files are deleted after this


class AriConfig(_Strict):
    # Who Ari is, in a few sentences (empty: the witty friend, worker/think.py PERSONA). Used in every reply.
    personality: str = ""
    call_me: str = ""  # what Ari calls you now and then (e.g. "Sas"); empty: no name
    # Ari's natural voice: a Piper voice file (.onnx, with its .onnx.json next to it) on the machine running argusd.
    # Empty: the browser's own voice.
    # Get one: python -m piper.download_voices en_US-lessac-medium --data-dir data/voices
    voice: str = ""
    # "expressive": Chatterbox on the PC's GPU (moods, real laughs and sighs; its own Python environment, see
    # docs/setup.md), with the Piper voice above as the fallback. "piper": Piper only.
    voice_engine: Literal["piper", "expressive"] = "piper"
    expressive_url: str = "http://127.0.0.1:8611"
    expressive_python: str = ".venv-voice/Scripts/python.exe"  # the environment Chatterbox is installed in
    expressive_model: Literal["turbo", "standard"] = "turbo"
    voice_clip: str = ""  # 5-15 s of the voice Ari should sound like (empty: a clip made from the Piper voice)
    # How Ari hears you: "browser" (the browser's speech recognition) or "whisper" (Whisper on the PC: private and
    # better with accents; the browser is used while the PC is off).
    hearing: Literal["browser", "whisper"] = "browser"
    whisper_model: str = "small.en"  # tiny.en, base.en, small.en, medium.en, large-v3 (bigger: better, slower)
    # "Hey Ari" on the PC's microphone, no browser (python -m argus.ari_listen; `dev.ps1 up` starts it when on).
    listen: bool = False
    listen_wake_model: str = "tiny.en"  # listens for the wake phrase (small and fast; the command uses whisper_model)
    follow_up: bool = True  # after Ari answers, keep listening a few seconds: carry on without "Hey Ari"
    # Talk like a conversation: after "Hey Ari" just talk back and forth, talk over Ari to interrupt, "thanks Ari"
    # ends it (also after talk_idle_s of quiet). Knows when you've finished a sentence (Smart Turn, downloaded
    # once). false: the classic mode (every request starts with "Hey Ari").
    live: bool = True
    talk_idle_s: int = Field(20, ge=5, le=300)
    # Names Ari should expect to hear (people, apps, places): Whisper is biased towards these spellings. Names in
    # what Ari remembers are added by themselves.
    vocabulary: list[str] = Field(default_factory=list)
    # What Whisper writes -> what you meant, e.g. {"kancha": "Kaancha"} (whole words, any case)
    heard_as: dict[str, str] = Field(default_factory=dict)
    # Ari says important things out loud at the PC (an overdue issue, a price drop, a failed backup): only while
    # you're at the PC, only between these hours, at most one every 10 minutes.
    speak_up: bool = True
    speak_hours: str = Field("08:00-22:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$")
    # Ari's popup over the whole screen while Ari listens, thinks or talks (python -m argus.ari_popup; needs
    # pip install -e .[popup]; `dev.ps1 up` starts it when on).
    popup: bool = False
    # The Ari pill's look: "pulse" (a soft light that breathes with the voice) or "comet" (a soft light travelling
    # round the edge). Each browser can pick its own on the Ari page.
    pill: Literal["pulse", "comet"] = "pulse"


class GuidanceConfig(_Strict):
    enabled: bool = True
    at: str = Field("03:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")  # the nightly review (needs Claude)
    keep_samples: int = Field(200, ge=10, le=5000)  # model answers kept per playbook (marked ones always stay)
    max_per_review: int = Field(12, ge=1, le=50)  # mistakes shown to Claude per playbook


class BriefConfig(_Strict):
    enabled: bool = True
    at: str = Field("07:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")  # local time: the phone gets the morning brief
    weather: str = Field("", max_length=80)  # a town ("Colombo", "Kandy, LK"): today's forecast in the brief


class SummaryConfig(_Strict):
    enabled: bool = True
    at: str = Field("20:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")  # the evening summary: today, time saved


class HealthConfig(_Strict):
    # The PC's worker checks WSL and Docker every `every_minutes` (while the PC is on; never wakes it) and tells the
    # phone when something changed: a container in `containers` stopped, one reports unhealthy, Docker is down.
    enabled: bool = False
    every_minutes: int = Field(30, ge=5, le=1440)
    containers: list[str] = Field(default_factory=list)  # must be running, e.g. [eclaire-app, eclaire-db]


class BackupConfig(_Strict):
    enabled: bool = True
    at: str = Field("02:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")  # local time, every night
    keep: int = Field(5, ge=1, le=100)  # newest backups kept (here and in copy_to)
    copy_to: str | None = None  # a folder on the PC, e.g. G:/ArgusBackups: the PC worker fetches each new backup


class PathsConfig(_Strict):
    allowed: list[Path] = Field(default_factory=list)
    blocked: list[Path] = Field(default_factory=list)


class Secrets(BaseModel):
    """Values from .env and the environment. Never logged."""

    model_config = ConfigDict(extra="ignore")
    admin_password: str | None = Field(None, repr=False)
    worker_token: str | None = Field(None, repr=False)
    ntfy_topic: str | None = Field(None, repr=False)
    ntfy_token: str | None = Field(None, repr=False)  # only for a private ntfy server with access control
    ntfy_reply_topic: str | None = Field(None, repr=False)  # optional; derived from the topic when unset
    ntfy_reply_write_token: str | None = Field(None, repr=False)  # ntfy mode with a login: write-only token
    webhooks: dict[str, str] = Field(default_factory=dict, repr=False)  # hook name -> its secret
    env: dict[str, str] = Field(default_factory=dict, repr=False)  # .env as read, for secrets named later


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
    windows: dict[str, str] = Field(default_factory=lambda: {"night": "01:00-06:00"})
    schedules: list[ScheduleConfig] = Field(default_factory=list)
    triggers: TriggersConfig = Field(default_factory=TriggersConfig)
    power: PowerConfig = Field(default_factory=PowerConfig)
    plugins: PluginsConfig = Field(default_factory=PluginsConfig)
    share: ShareConfig = Field(default_factory=ShareConfig)
    ari: AriConfig = Field(default_factory=AriConfig)
    brief: BriefConfig = Field(default_factory=BriefConfig)
    guidance: GuidanceConfig = Field(default_factory=GuidanceConfig)
    summary: SummaryConfig = Field(default_factory=SummaryConfig)
    health: HealthConfig = Field(default_factory=HealthConfig)
    backup: BackupConfig = Field(default_factory=BackupConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)

    def model_post_init(self, _ctx) -> None:
        from .cron import CronError, parse_window

        for name, text in self.windows.items():
            try:
                parse_window(text)
            except CronError as e:
                raise ValueError(f"windows.{name}: {e}") from None
        specs = [*self.schedules, *self.triggers.folders, *self.triggers.webhooks]
        for spec in specs:
            if spec.window and spec.window not in self.windows:
                raise ValueError(f"window {spec.window!r} is not defined under windows")
        for kind, items in (("schedule", [s.id for s in self.schedules]),
                            ("folder trigger", [f.name for f in self.triggers.folders]),
                            ("webhook", [w.name for w in self.triggers.webhooks])):
            dup = {x for x in items if items.count(x) > 1}
            if dup:
                raise ValueError(f"{kind} names must be unique: {', '.join(sorted(dup))}")

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

    @property
    def log_dir(self) -> Path:
        """Where every process's log lives (argusd's, the worker's, Ollama's when `up` started it)."""
        return self.log_path.parent if self.log_path else self.resolve(Path("logs"))


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for lineno, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
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
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8-sig")) or {}
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
        admin_password=env.get("ARGUS_ADMIN_PASSWORD") or None,
        worker_token=env.get("ARGUS_WORKER_TOKEN") or None,  # an empty value means "not set"
        ntfy_topic=env.get("NTFY_TOPIC") or None,
        ntfy_token=env.get("NTFY_TOKEN") or None,
        ntfy_reply_topic=env.get("NTFY_REPLY_TOPIC") or None,
        ntfy_reply_write_token=env.get("NTFY_REPLY_WRITE_TOKEN") or None,
    )
    cfg.secrets.env = env
    missing = []
    for hook in cfg.triggers.webhooks:
        secret = env.get(hook.secret_env) or os.environ.get(hook.secret_env)
        if not secret or len(secret) < 16:
            missing.append(hook.secret_env)
        else:
            cfg.secrets.webhooks[hook.name] = secret
    if missing:
        raise ConfigError("Webhook secrets missing or shorter than 16 characters in .env: " + ", ".join(missing))
    if not cfg.secrets.worker_token and cfg.server.host not in ("127.0.0.1", "localhost", "::1"):
        raise ConfigError(
            f"server.host is {cfg.server.host}, but ARGUS_WORKER_TOKEN is not set in .env.\n"
            "  Argus refuses to serve its API beyond this computer without a token."
        )
    return cfg
