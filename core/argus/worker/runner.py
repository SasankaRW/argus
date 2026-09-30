"""The worker loop: register, claim a job, run its workflow, report, repeat.

Safety rules:
- A heartbeat thread keeps the lease alive while a job runs. If Argus says the lease is gone, the worker
  stops that job at the next step boundary and does not report anything else for it.
- Crashes are fine: the lease expires, the watchdog requeues the job and the next worker skips finished steps.
- Ctrl+C when idle: exit now. During a job: finish it, then exit. Twice: exit now (the job is retried later).
"""

from __future__ import annotations

import logging
import socket
import threading
import time
import traceback
import urllib.parse
from typing import Any

from .. import __version__
from ..models import Router, build_providers
from . import ask as _ask  # noqa: F401 - Ask Argus (the model part)
from . import backup as _backup  # noqa: F401 - the PC's copy of the nightly backup
from . import health as _health  # noqa: F401 - WSL and Docker on the PC
from . import hear as _hear  # noqa: F401 - Ari's hearing (Whisper)
from . import power as _power  # noqa: F401 - the built-in power buttons (sleep, shut down, ...)
from .client import ApiError, ArgusClient, LeaseLostError, Unreachable
from .plugins import Files, Http, LoadedPlugin, Secrets, Store
from .plugins import load as load_plugins
from .watcher import start_watchers
from .workflows import REGISTRY, Context, PermanentError, WaitSignal, WorkflowRegistry

log = logging.getLogger("argus.worker")


class _JobReporter:
    def __init__(self, client: ArgusClient, job_id: str, worker_id: str):
        self.client = client
        self.job_id = job_id
        self.worker_id = worker_id
        self._lost = threading.Event()

    def step(self, idx: int, name: str, state: str, *, output: Any = None, error: str | None = None,
             tier: str | None = None) -> None:
        try:
            self.client.step(self.job_id, self.worker_id, idx, name, state, output=output, error=error, tier=tier)
        except LeaseLostError:
            self._lost.set()
            raise

    def approval(self, body: dict) -> dict:
        return self._call(lambda: self.client.approval(self.job_id, self.worker_id, body))

    def notify(self, body: dict) -> dict:
        return self._call(lambda: self.client.notify(self.job_id, self.worker_id, body))

    def _call(self, fn):
        try:
            return fn()
        except LeaseLostError:
            self._lost.set()
            raise

    def mark_lost(self) -> None:
        self._lost.set()

    @property
    def lease_lost(self) -> bool:
        return self._lost.is_set()


class _JobBoard:
    """The router's link to argusd for one job: permits, breaker reports and trace events."""

    def __init__(self, client: ArgusClient, worker_id: str, job_id: str, ctx: Context, jlog):
        self.client, self.worker, self.job_id, self.ctx, self.log = client, worker_id, job_id, ctx, jlog

    def permit(self, tier: str) -> dict:
        return self.client.permit(tier, self.worker, self.job_id)

    def report(self, tier: str, ok: bool, latency_ms: float | None, error: str | None) -> None:
        try:
            self.client.report(tier, self.worker, self.job_id, ok, latency_ms, error)
        except ApiError as e:  # the breaker is advisory; a failed report must not fail the step
            self.log.warning("model report failed", extra={"tier": tier, "error": str(e)})

    def event(self, kind: str, src: str | None, dst: str | None, data: dict) -> None:
        self.client.trace(self.job_id, self.worker, kind, src=src, dst=dst, step=self.ctx._step, data=data)


class _Heartbeat(threading.Thread):
    def __init__(self, client: ArgusClient, reporter: _JobReporter, every: float):
        super().__init__(name=f"heartbeat-{reporter.job_id}", daemon=True)
        self.client = client
        self.reporter = reporter
        self.every = every
        self._halt = threading.Event()

    def run(self) -> None:
        while not self._halt.wait(self.every):
            try:
                self.client.heartbeat(self.reporter.job_id, self.reporter.worker_id)
            except LeaseLostError:
                log.warning("lease lost", extra={"job": self.reporter.job_id})
                self.reporter.mark_lost()
                return
            except (ApiError, Unreachable) as e:  # keep trying; the lease is long enough for a few misses
                log.warning("heartbeat failed", extra={"job": self.reporter.job_id, "error": str(e)})

    def stop(self) -> None:
        self._halt.set()


class Worker:
    def __init__(self, client: ArgusClient, worker_id: str | None = None, *, capabilities: list[str] | None = None,
                 registry: WorkflowRegistry | None = None, host: str | None = None, claim_wait: float = 10.0,
                 ollama_url: str | None = None, providers: dict | None = None, watch_folders: bool = True):
        self.client = client
        self.registry = registry or REGISTRY
        self.host = host or socket.gethostname()
        self.id = worker_id or f"worker-{self.host}".lower()
        self.capabilities = sorted(set(capabilities or []) | set(self.registry.needs))
        self.claim_wait = claim_wait
        self.heartbeat_seconds = 15.0
        self.ollama_url = ollama_url
        self.models_cfg: dict = {}
        self.providers: dict = providers if providers is not None else {}
        self._fixed_providers = providers is not None  # tests pass their own
        self.stopping = threading.Event()
        self.jobs_done = 0
        self.busy = False  # True while a job runs
        self.watch_folders = watch_folders
        self.watchers: list = []  # folder watchers for this worker's folder triggers
        self.plugins: dict[str, LoadedPlugin] = {}  # plugins loaded from their folders, by id
        self.plugin_errors: dict[str, str] = {}
        self.path_rules: dict[str, list[str]] = {}

    # -------------------------------------------------------------- lifecycle

    def register(self) -> None:
        info = self.client.register(self.id, self.host, self.capabilities, __version__)
        self.heartbeat_seconds = float(info.get("heartbeat_seconds", self.heartbeat_seconds))
        self.models_cfg = info.get("models") or {}
        if not self._fixed_providers:
            self.providers = build_providers(self.models_cfg, ollama_url=self.ollama_url)
        self.path_rules = info.get("paths") or {}
        self._load_plugins(info.get("plugins") or [])
        folders = info.get("folders") or []
        if self.watch_folders and [w.t for w in self.watchers] != folders:
            for w in self.watchers:
                w.stop()
            self.watchers = start_watchers(self.client, self.id, folders)
        log.info("worker registered", extra={"worker": self.id, "capabilities": self.capabilities,
                                             "plugins": self.registry.plugins})

    def _load_plugins(self, infos: list[dict[str, Any]]) -> None:
        """Import new plugins; refresh the settings (live, config, permissions) of ones already loaded."""
        new = [i for i in infos if i["id"] not in self.plugins]
        for i in infos:
            if i["id"] in self.plugins:
                self.plugins[i["id"]] = LoadedPlugin(i)
        if new:
            loaded, errors = load_plugins(new, self.registry)
            self.plugins.update(loaded)
            self.plugin_errors.update(errors)
            for pid, err in errors.items():
                log.error("plugin not loaded", extra={"plugin": pid, "error": err})
        self.capabilities = sorted(set(self.capabilities) | set(self.registry.needs))

    def run_forever(self, max_jobs: int | None = None) -> None:
        self.register()
        while not self.stopping.is_set():
            if max_jobs is not None and self.jobs_done >= max_jobs:
                return
            self.run_once()

    CLAUDE_CHECK_SECONDS = 1800.0
    _claude_checked = 0.0

    def check_claude(self, force: bool = False) -> None:
        """Every half hour: is the Claude CLI on this machine logged in? (argusd tells the phone when it isn't.)"""
        if not force and time.monotonic() - self._claude_checked < self.CLAUDE_CHECK_SECONDS:
            return
        self._claude_checked = time.monotonic()
        claude = next((p for p in self.providers.values() if getattr(p, "kind", "") == "claude"), None)
        if claude is None or not hasattr(claude, "auth_status"):
            return
        logged_in, detail = claude.auth_status()
        if logged_in is None:
            return
        try:
            self.client.post(f"/workers/{self.id}/claude", {"logged_in": logged_in, "detail": detail})
        except (Unreachable, ApiError) as e:
            log.warning("could not report the Claude login", extra={"error": str(e)})

    def run_once(self, wait: float | None = None) -> bool:
        """Claim and run at most one job. Returns True if a job was run."""
        self.check_claude()
        try:
            job = self.client.claim(self.id, self.capabilities, self.registry.plugins,
                                    self.claim_wait if wait is None else wait)
        except Unreachable as e:
            log.warning("cannot reach argus", extra={"error": str(e)})
            self.stopping.wait(5)
            return False
        except ApiError as e:
            if e.status == 404:  # Argus forgot us (fresh database): register again
                self.register()
                return False
            log.warning("argus refused the claim", extra={"status": e.status, "error": str(e)})
            self.stopping.wait(5)
            return False
        if job is None:
            return False
        self.busy = True
        try:
            self.run_job(job)
        finally:
            self.busy = False
        self.jobs_done += 1
        return True

    # -------------------------------------------------------------- one job

    def run_job(self, job: dict[str, Any]) -> str:
        """Run a claimed job. Returns what happened: succeeded, waiting, failed, dead, or lost."""
        job_id = job["id"]
        jlog = logging.LoggerAdapter(log, {"job": job_id, "plugin": job["plugin"], "workflow": job["workflow"]})
        wf = self.registry.get(job["plugin"], job["workflow"])
        try:
            self.client.start(job_id, self.id)
        except LeaseLostError:
            jlog.warning("lease lost before start")
            return "lost"
        except (Unreachable, ApiError) as e:  # argus is restarting: the lease runs out and the job comes back
            jlog.warning("could not start the job; argus will hand it out again", extra={"error": str(e)})
            return "lost"

        if wf is None:
            why = f"unknown workflow {job['plugin']}.{job['workflow']}"
            self._report(jlog, lambda: self.client.fail(job_id, self.id, why, False))
            jlog.error("unknown workflow")
            return "dead"

        reporter = _JobReporter(self.client, job_id, self.id)
        beat = _Heartbeat(self.client, reporter, self.heartbeat_seconds)
        beat.start()
        ctx = Context(job, reporter, logging.getLogger(f"argus.plugin.{job['plugin']}"))
        ctx._router = Router(self.providers, _JobBoard(self.client, self.id, job_id, ctx, jlog),
                             chain=self.models_cfg.get("chain") or sorted(self.providers),
                             attempts_per_tier=self.models_cfg.get("attempts_per_tier", 2),
                             source=job["plugin"])
        info = job.get("plugin_info")
        if info and info["id"] in self.plugins:  # argusd's settings now (dry-run or live, config)
            self.plugins[info["id"]] = LoadedPlugin(info)
        if job["plugin"] == "ari":  # built in: the recording to transcribe
            ctx.shared = lambda name: self.client.get_bytes(f"/ari/audio/{urllib.parse.quote(name, safe='')}")
        if job["plugin"] == "backup":  # built in: fetch backups from argusd
            ctx.shared_backup = lambda name: self.client.get_bytes(f"/backups/files/{name}", timeout=600)
        plugin = self.plugins.get(job["plugin"])
        if plugin is not None:
            self._attach(ctx, plugin, job_id)
        jlog.info("job started", extra={"attempt": job.get("attempt"), "checkpoints": len(ctx._done)})
        try:
            result = wf.fn(ctx)
            ctx._flush_trace()
            if reporter.lease_lost:
                raise LeaseLostError(409, {"error": "lease_lost"})
            self.client.succeed(job_id, self.id, result)
            jlog.info("job succeeded", extra={"skipped_steps": ctx.skipped})
            return "succeeded"
        except LeaseLostError:
            jlog.warning("lease lost; leaving the job to argus")
            return "lost"
        except WaitSignal as w:
            reason = w.reason
            self._report(jlog, lambda: self.client.wait(job_id, self.id, reason))
            jlog.info("job waiting", extra={"reason": reason})
            return "waiting"
        except PermanentError as e:
            why = str(e) or type(e).__name__
            self._report(jlog, lambda: self.client.fail(job_id, self.id, why, False))
            jlog.error("job failed permanently", extra={"error": why})
            return "dead"
        except Exception as e:
            detail = f"{type(e).__name__}: {e}"
            self._report(jlog, lambda: self.client.fail(job_id, self.id, detail, True))
            jlog.error("job failed", extra={"error": detail, "trace": traceback.format_exc(limit=5)})
            return "failed"
        finally:
            if ctx._unsent:
                ctx._flush_trace()
            beat.stop()

    def _attach(self, ctx: Context, plugin: LoadedPlugin, job_id: str) -> None:
        """Give the job the services its manifest allows, and nothing more."""
        client, wid, pid = self.client, self.id, plugin.id

        unsent: list[tuple[str, str | None, dict]] = []  # events argusd didn't take yet (it was restarting)
        ctx._unsent = unsent

        def send(kind: str, step: str | None, data: dict) -> bool:
            for wait in (0, 1, 3):
                time.sleep(wait)
                try:
                    client.trace(job_id, wid, kind, src=pid, dst=None, step=step, data=data)
                    return True
                except LeaseLostError:
                    raise
                except (Unreachable, ApiError) as e:
                    err = str(e)
            log.warning("could not record a plugin event yet; kept to send later", extra={"kind": kind, "error": err})
            return False

        def trace(kind: str, data: dict) -> None:
            """Record a change (the undo log). Kept and sent later when argusd can't take it now, never lost."""
            while unsent and send(*unsent[0]):
                unsent.pop(0)
            item = (kind, ctx._step, data)
            if unsent or not send(*item):
                unsent.append(item)

        def flush() -> None:
            while unsent and send(*unsent[0]):
                unsent.pop(0)
            if unsent:
                log.error("plugin events could not be recorded (undo won't list them)",
                          extra={"lost": [k for k, _, _ in unsent]})

        ctx._flush_trace = flush

        perms = plugin.perms
        ctx.plugin = plugin
        ctx.config = dict(plugin.config)
        ctx.dry_run = plugin.dry_run
        ctx.files = Files(pid, perms.get("files") or {}, self.path_rules, plugin.dry_run, trace)
        ctx.http = Http(pid, perms.get("network") or [], trace)
        ctx.secrets = Secrets(pid, perms.get("secrets") or [])
        ctx.store = Store(client, pid)
        ctx.emit = lambda name, **data: trace(f"plugin.{name}", data)
        share = (ctx.input or {}).get("share")
        if share:  # something sent from the phone's share menu: its files come from argusd
            ctx.shared = lambda name: client.get_bytes(
                f"/shares/{urllib.parse.quote(str(share))}/files/{urllib.parse.quote(name, safe='')}")
        if ctx._router is not None:  # start at the lowest tier the manifest lists; higher ones by escalation
            chain = list(ctx._router.chain)
            starts = [chain.index(t) for t in perms.get("models") or [] if t in chain]
            outside = [t for t in perms.get("models") or [] if t not in chain]  # e.g. V1, asked for directly
            ctx._allowed_tiers = (chain[min(starts):] if starts else []) + outside

    @staticmethod
    def _report(jlog: logging.LoggerAdapter, fn) -> None:
        try:
            fn()
        except LeaseLostError:
            jlog.warning("lease lost while reporting")
        except (Unreachable, ApiError) as e:
            # Argus is restarting or refused it: the lease runs out and the job is retried from its last step.
            # The worker keeps running instead of exiting.
            jlog.warning("could not report the job's outcome; argus will retry it", extra={"error": str(e)})
