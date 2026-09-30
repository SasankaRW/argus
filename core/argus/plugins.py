"""The plugin host (argusd side): find plugin folders, check their manifests, and wire them into Argus.

A plugin is a folder with a `plugin.yaml` (the manifest) and a `plugin.py` (its workflows, registered with
`@workflow("<plugin id>", "<name>")`). argusd never runs plugin code: it reads manifests, puts the plugin on the map,
turns its triggers into schedules, folder watches and webhooks, and tells workers which plugins to load. Workers
import `plugin.py` and give each job a `ctx` limited to what the manifest declares (files, network, models,
Claude calls, secrets).

A new plugin starts in dry-run: it runs, logs what it would do, and changes nothing until you list it under
`plugins.live` in argus.yaml.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .config import (
    PRIORITY_INTERACTIVE,
    PRIORITY_SCHEDULED,
    Config,
    FolderTrigger,
    ScheduleConfig,
    WebhookTrigger,
    _check_cron,
)

log = logging.getLogger("argus.plugins")

API_VERSION = (1, 0)  # the plugin API this Argus provides (Plugin Guide: "Plugin API changelog")
ID = r"^[a-z0-9][a-z0-9-]{0,62}$"
TIERS_PATTERN = r"^[A-Z]\d$"  # T1..T3 text, V1 vision


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------ triggers


class ScheduleTrigger(_M):
    workflow: str
    cron: str
    window: str | None = None
    input: dict = Field(default_factory=dict)

    @field_validator("cron")
    @classmethod
    def _cron(cls, v: str) -> str:
        return _check_cron(v)


class FolderWatch(_M):
    workflow: str
    paths: list[str] = Field(min_length=1)
    match: list[str] | str = "*"
    stable_for: str | int = "2m"  # seconds, or "90s" / "2m"
    recursive: bool = False

    def seconds(self) -> float:
        v = self.stable_for
        if isinstance(v, int):
            return float(v)
        m = re.fullmatch(r"\s*(\d+)\s*([sm]?)\s*", v)
        if not m:
            raise ValueError(f"stable_for {v!r}: use seconds or e.g. 90s / 2m")
        return float(m.group(1)) * (60 if m.group(2) == "m" else 1)


class Webhook(_M):
    workflow: str
    secret_env: str
    style: Literal["argus", "github"] = "argus"


class Manual(_M):
    workflow: str
    label: str


class Trigger(_M):
    schedule: ScheduleTrigger | None = None
    folder_watch: FolderWatch | None = None
    webhook: Webhook | None = None
    manual: Manual | None = None

    @model_validator(mode="after")
    def _one(self) -> Trigger:
        if sum(x is not None for x in (self.schedule, self.folder_watch, self.webhook, self.manual)) != 1:
            raise ValueError("each trigger is exactly one of schedule, folder_watch, webhook, manual")
        return self

    def workflow(self) -> str:
        t = self.schedule or self.folder_watch or self.webhook or self.manual
        return t.workflow  # type: ignore[union-attr]


# ------------------------------------------------------------------ permissions and the manifest


class FilePerms(_M):
    read: list[str] = Field(default_factory=list)
    write: list[str] = Field(default_factory=list)
    delete: Literal["none", "recycle_bin"] = "none"


class Permissions(_M):
    files: FilePerms = Field(default_factory=FilePerms)
    models: list[str] = Field(default_factory=lambda: ["T0"])  # the tiers it starts at; higher only by escalation
    claude_calls_per_day: int | None = Field(None, ge=0, le=1000)  # None: claude.plugin_calls_per_day
    network: list[str] = Field(default_factory=list)  # host names it may call through ctx.http
    uses: list[str] = Field(default_factory=list)  # connector actions (later)
    secrets: list[str] = Field(default_factory=list)  # .env names it may read through ctx.secrets

    @field_validator("models")
    @classmethod
    def _tiers(cls, v: list[str]) -> list[str]:
        for t in v:
            if not re.fullmatch(TIERS_PATTERN, t):
                raise ValueError(f"{t!r} is not a tier name like T1 or V1")
        return v


class ShareTarget(_M):
    """What the plugin can take from the phone's share menu (Helios > Share)."""
    workflow: str
    label: str
    accepts: list[Literal["file", "image", "pdf", "url", "text"]] = Field(min_length=1)


class ConfigField(_M):
    type: Literal["list", "int", "text", "bool", "choice", "path"] = "text"
    default: Any = None
    choices: list[str] | None = None
    label: str | None = None


class HeliosNode(_M):
    label: str | None = None
    group: str | None = None
    icon: str | None = None


class WrongButton(_M):
    """A "Wrong" button on each change a job made: you pick the right answer, the plugin fixes the file and keeps
    the correction as an example for next time. The choices come from the plugin's state key `choices`."""
    workflow: str
    label: str = "Wrong"


class RulesFile(_M):
    """A YAML file of rules (in the plugin folder) that you can read and edit in Helios. Your edited copy is kept
    by argusd (plugin state `rules`); the plugin reads it at the start of each job, falling back to the file."""
    file: str = Field("rules.yaml", pattern=r"^[A-Za-z0-9_.-]+\.ya?ml$")
    label: str = "Rules"


class HeliosInfo(_M):
    node: HeliosNode = Field(default_factory=HeliosNode)
    wrong: WrongButton | None = None
    rules: RulesFile | None = None


class Manifest(_M):
    id: str = Field(pattern=ID)
    name: str
    version: str
    kind: Literal["workflow", "connector", "trigger"]
    argus_api: str
    description: str = ""
    runs_on: Literal["laptop", "desktop", "any"] = "any"
    needs: list[str] = Field(default_factory=list)
    triggers: list[Trigger] = Field(default_factory=list)
    workflows: list[str] = Field(default_factory=list)  # names; defaults to those the triggers use
    permissions: Permissions
    approvals: list[dict] = Field(default_factory=list)
    config: dict[str, ConfigField] = Field(default_factory=dict)
    helios: HeliosInfo = Field(default_factory=HeliosInfo)
    share: list[ShareTarget] = Field(default_factory=list)

    @model_validator(mode="after")
    def _checks(self) -> Manifest:
        if self.kind != "connector" and not self.triggers:
            raise ValueError("a workflow or trigger plugin needs at least one trigger")
        if not api_ok(self.argus_api):
            raise ValueError(f"argus_api {self.argus_api!r} does not include this Argus "
                             f"(plugin API {API_VERSION[0]}.{API_VERSION[1]})")
        for t in self.triggers:
            if t.folder_watch:
                t.folder_watch.seconds()  # raises on a bad value
        return self

    def all_workflows(self) -> list[str]:
        extra = ({self.helios.wrong.workflow} if self.helios.wrong else set()) | {t.workflow for t in self.share}
        return sorted(set(self.workflows) | {t.workflow() for t in self.triggers} | extra)

    def job_needs(self) -> list[str]:
        """What a worker must offer to run this plugin's jobs."""
        extra = ["desktop"] if self.runs_on == "desktop" else ["laptop"] if self.runs_on == "laptop" else []
        return sorted(set([n for n in self.needs if n != "none"] + extra))


def api_ok(spec: str) -> bool:
    """">=1.0 <2.0" style ranges (space-separated conditions, all must hold)."""
    have = API_VERSION
    for cond in spec.split():
        m = re.fullmatch(r"(>=|<=|>|<|==)?(\d+)\.(\d+)", cond)
        if not m:
            return False
        op, want = m.group(1) or "==", (int(m.group(2)), int(m.group(3)))
        ok = {">=": have >= want, "<=": have <= want, ">": have > want, "<": have < want, "==": have == want}[op]
        if not ok:
            return False
    return True


class Plugin(BaseModel):
    manifest: Manifest
    path: Path
    live: bool
    config: dict[str, Any]

    def info(self) -> dict[str, Any]:
        m = self.manifest
        return {"id": m.id, "name": m.name, "version": m.version, "kind": m.kind, "description": m.description,
                "path": str(self.path), "live": self.live, "runs_on": m.runs_on, "needs": m.job_needs(),
                "workflows": m.all_workflows(), "permissions": m.permissions.model_dump(), "config": self.config,
                "triggers": [t.model_dump(exclude_none=True) for t in m.triggers],
                "wrong": m.helios.wrong.model_dump() if m.helios.wrong else None,
                "share": [t.model_dump() for t in m.share],
                "rules": m.helios.rules.label if m.helios.rules else None}


# ------------------------------------------------------------------ loading


def discover(cfg: Config) -> tuple[list[Plugin], dict[str, str]]:
    """Read every plugin folder. Returns (valid plugins, {folder: error}) - a bad plugin never stops Argus."""
    plugins: list[Plugin] = []
    errors: dict[str, str] = {}
    seen: set[str] = set()
    for d in cfg.plugins.dirs:
        root = cfg.resolve(d)
        if not root.is_dir():
            continue
        for folder in sorted(p for p in root.iterdir() if p.is_dir() and (p / "plugin.yaml").exists()):
            try:
                raw = yaml.safe_load((folder / "plugin.yaml").read_text(encoding="utf-8-sig")) or {}
                m = Manifest.model_validate(raw)
            except (yaml.YAMLError, ValidationError, ValueError) as e:
                errors[str(folder)] = _short(e)
                log.error("plugin manifest rejected", extra={"folder": str(folder), "error": errors[str(folder)]})
                continue
            if m.id != folder.name:
                errors[str(folder)] = f"folder name must equal the plugin id ({m.id})"
                continue
            if m.id in seen:
                errors[str(folder)] = f"plugin id {m.id} is used twice"
                continue
            if not (folder / "plugin.py").exists():
                errors[str(folder)] = "plugin.py is missing"
                continue
            seen.add(m.id)
            conf = {k: f.default for k, f in m.config.items()}
            conf.update(cfg.plugins.config.get(m.id, {}))
            plugins.append(Plugin(manifest=m, path=folder, live=m.id in cfg.plugins.live, config=conf))
    return plugins, errors


def _short(e: Exception) -> str:
    if isinstance(e, ValidationError):
        return "; ".join(f"{'.'.join(map(str, x['loc'])) or 'manifest'}: {x['msg']}" for x in e.errors()[:5])
    return str(e)[:500]


def wire(cfg: Config, plugins: list[Plugin]) -> None:
    """Turn manifest triggers into the scheduler's schedules and the trigger config. Ids are prefixed with the
    plugin id, so they never clash with each other or with argus.yaml."""
    for p in plugins:
        m = p.manifest
        needs = m.job_needs()
        for i, t in enumerate(m.triggers):
            if t.schedule:
                s = t.schedule
                cfg.schedules.append(ScheduleConfig(
                    id=f"{m.id}.{s.workflow}.{i}", plugin=m.id, workflow=s.workflow, cron=s.cron, input=s.input,
                    needs=needs, window=s.window, priority=PRIORITY_SCHEDULED))
            elif t.folder_watch:
                fw = t.folder_watch
                for j, path in enumerate(fw.paths):
                    cfg.triggers.folders.append(FolderTrigger(
                        name=f"{m.id}.{i}.{j}", plugin=m.id, workflow=fw.workflow, path=path,
                        worker="@desktop" if m.runs_on == "desktop" else "@laptop" if m.runs_on == "laptop"
                        else "@any", patterns=[fw.match] if isinstance(fw.match, str) else fw.match,
                        settle_seconds=fw.seconds(), recursive=fw.recursive, needs=needs))
            elif t.webhook:
                cfg.triggers.webhooks.append(WebhookTrigger(
                    name=m.id if sum(1 for x in m.triggers if x.webhook) == 1 else f"{m.id}.{i}", plugin=m.id,
                    workflow=t.webhook.workflow, secret_env=t.webhook.secret_env, style=t.webhook.style,
                    needs=needs, priority=PRIORITY_INTERACTIVE))


class PluginHost:
    """Loads manifests at argusd start and answers what workers and the API need."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.plugins: dict[str, Plugin] = {}
        self.errors: dict[str, str] = {}

    def load(self) -> None:
        found, self.errors = discover(self.cfg)
        ok = []
        for p in found:
            missing = [t.webhook.secret_env for t in p.manifest.triggers
                       if t.webhook and len(self.cfg.secrets.env.get(t.webhook.secret_env, "")) < 16]
            if missing:
                self.errors[str(p.path)] = "webhook secret missing or shorter than 16 characters in .env: " + \
                    ", ".join(missing)
                continue
            ok.append(p)
        try:
            wire(self.cfg, ok)
        except (ValidationError, ValueError) as e:  # e.g. a trigger using a window argus.yaml lacks
            self.errors["(triggers)"] = _short(e)
        self.plugins = {p.manifest.id: p for p in ok}
        log.info("plugins loaded", extra={"plugins": sorted(self.plugins), "rejected": len(self.errors)})

    def claude_caps(self) -> dict[str, int]:
        default = self.cfg.claude.plugin_calls_per_day
        return {pid: default if p.manifest.permissions.claude_calls_per_day is None
                else p.manifest.permissions.claude_calls_per_day for pid, p in self.plugins.items()}

    def needs_for(self, plugin: str) -> list[str]:
        p = self.plugins.get(plugin)
        return p.manifest.job_needs() if p else []

    def for_worker(self, capabilities: list[str]) -> list[dict[str, Any]]:
        """Plugins this worker can run (it has every capability their jobs need)."""
        caps = set(capabilities)
        return [p.info() for p in self.plugins.values() if set(p.manifest.job_needs()) <= caps]

    def list(self) -> dict[str, Any]:
        return {"plugins": [p.info() for p in self.plugins.values()],
                "errors": [{"folder": k, "error": v} for k, v in self.errors.items()]}
