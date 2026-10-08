"""The running Argus instance: config, store, jobs and background tasks, started and stopped together."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from datetime import datetime

from . import __version__
from . import settings as settings_mod
from .approvals import Approvals
from .backup import Backups
from .config import PRIORITY_BATCH, PRIORITY_INTERACTIVE, Config, ScheduleConfig
from .daily import (
    Marker,
    brief_due,
    brief_parts,
    compose_brief,
    compose_summary,
    last_health,
    resume_summary,
    say_brief,
)
from .db import Store
from .events import EventHub, insert_event, prune_events
from .jobs import JobStore, Watchdog
from .modelboard import ModelBoard
from .outbox import Outbox, add_message, phone_message
from .plugins import PluginHost
from .power import PowerManager
from .presence import PhoneWatch
from .registry import Registry
from .scheduler import Scheduler
from .triggers import Triggers
from .weather import Weather, words
from .weather import spoken as weather_spoken

log = logging.getLogger("argus")


class Argus:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.version = __version__
        self.store = Store(cfg.db_path)
        self.jobs = JobStore(self.store, cfg.jobs, windows=cfg.windows)
        self.registry = Registry(self.store)
        self.models = ModelBoard(self.store, cfg)
        self.outbox = Outbox(self.store, cfg)
        self.approvals = Approvals(self.store, self.jobs, cfg)
        self.phone = PhoneWatch(self.store, cfg, self.approvals, self.outbox)
        self.plugin_host = PluginHost(cfg)
        self.power = PowerManager(self.store, cfg)
        self.power.jobs = self.jobs
        self.scheduler = Scheduler(self.store, self.jobs, cfg)
        self.scheduler.poke = self.outbox.poke
        self.models.poke = self.outbox.poke
        self.triggers = Triggers(self.store, self.jobs, cfg)
        self.hub = EventHub(self.store, queue_size=cfg.events.stream_queue)
        stale_after = cfg.jobs.heartbeat_seconds * 4
        self.watchdog = Watchdog(
            self.jobs,
            cfg.jobs.watchdog_interval_seconds,
            extra=[lambda: self.registry.mark_stale_workers(stale_after), self.prune_events,
                   self.approval_tick, self.scheduler.tick,
                   self.power.tick, self.backup_tick, self.daily_tick],
        )
        self.backups = Backups(cfg.db_path, cfg.backup.keep)
        self.last_backup: dict | None = None
        self.marker = Marker(cfg.db_path.parent / "argus.running")
        self.brief_sent: str | None = None
        self.summary_sent: str | None = None
        self.guidance_sent: str | None = None
        self.health_sent: str | None = None
        self.health_tools: Callable[[], list[dict]] = lambda: []  # Ari's tools (set by the API)
        self.listener_seen: Callable[[], float] = lambda: 0.0  # when the PC's listener last said it runs
        self.listener_info: Callable[[], dict] = lambda: {}  # its mic and loudest level (the health check)
        self._next_touch = 0.0
        self.resumed: dict | None = None  # set at start when the last run ended abruptly
        self.started_at: float | None = None
        self._next_prune = 0.0
        self.pruned_events = 0
        self.weather = Weather()
        self.base_settings = settings_mod.base_values(cfg)  # argus.yaml's values, before Helios's overrides

    def model_needs(self) -> list[str]:
        """What a worker must offer to run model work (models.needs: [gpu] on the laptop)."""
        return list(self.cfg.models.needs)

    def open(self) -> Argus:
        """Synchronous part of startup: open the database and run migrations."""
        self.store.open()
        return self

    async def start(self) -> None:
        """Async part of startup: background tasks."""
        group = "laptop" if self.cfg.instance.host == "laptop" else "pc"
        await self.registry.component("argus", "core", "Argus", group, {"version": self.version})
        await self.models.register()
        await self.approvals.start()
        stopped_at = self.marker.start()
        if stopped_at is not None:  # the last run ended abruptly (power cut, crash): say what happens now
            await self.resume_notice(stopped_at)
        await self.apply_argus_settings()
        self.plugin_host.load()  # adds the plugins' schedules and triggers before the scheduler reads them
        self._health_schedule()
        await self.apply_plugin_settings()
        await self.register_plugins()
        self.models.plugin_caps = self.plugin_host.claude_caps()
        await self.scheduler.sync()
        await self.power.start()
        await self.outbox.start()
        await self.phone.start()
        await self.hub.start()
        self.watchdog.start()
        self.started_at = time.time()
        log.info("argus started", extra={"version": self.version, "instance": self.cfg.instance.name})

    def _setting_changed(self, key: str, value) -> None:
        if key == "backup.keep":
            self.backups.keep = value

    async def apply_argus_settings(self) -> None:
        """Settings changed in Helios, over argus.yaml (a value that no longer passes is skipped and logged)."""
        for key, value in (await self.store.read(settings_mod.load)).items():
            try:
                settings_mod.apply(self.cfg, key, settings_mod.check(self.cfg, key, value), self._setting_changed)
            except settings_mod.SettingError as e:
                log.warning("setting from Helios skipped", extra={"error": str(e)})

    async def change_settings(self, changes: dict) -> None:
        """Change settings now and keep them (None: back to argus.yaml). All are checked before any is changed."""
        checked = {k: (None if v is None else settings_mod.check(self.cfg, k, v)) for k, v in changes.items()}

        def fn(conn):
            over = settings_mod.load(conn)
            for k, v in checked.items():
                if v is None or v == self.base_settings[k]:
                    over.pop(k, None)
                else:
                    over[k] = v
            settings_mod.save(conn, over)
            insert_event(conn, time.time(), "settings.changed", src="helios", dst="argus",
                         data={"keys": sorted(checked)})

        await self.store.write(fn)
        for k, v in checked.items():
            settings_mod.apply(self.cfg, k, self.base_settings[k] if v is None else v, self._setting_changed)

    def _health_schedule(self) -> None:
        """health.enabled: the PC's worker checks WSL and Docker every health.every_minutes."""
        h = self.cfg.health
        if not h.enabled or any(s.id == "argus-health" for s in self.cfg.schedules):
            return
        n = h.every_minutes
        cron = f"*/{n} * * * *" if n < 60 else f"0 */{max(1, n // 60)} * * *"
        self.cfg.schedules.append(ScheduleConfig(id="argus-health", plugin="health", workflow="check", cron=cron,
                                                 input={"containers": h.containers}, needs=["desktop"],
                                                 priority=PRIORITY_BATCH))

    async def resume_notice(self, stopped_at: float) -> None:
        def fn(conn):
            now = time.time()
            title, text, data = resume_summary(conn, now, stopped_at)
            insert_event(conn, now, "argus.resumed", src="argus", dst="phone", data=data)
            add_message(conn, now, "phone", phone_message(title, text, tags=["electric_plug"]),
                        dedupe_key=f"resumed:{int(stopped_at)}")
            return data

        self.resumed = await self.store.write(fn)
        log.warning("argus did not stop cleanly last time", extra=self.resumed)

    async def daily_tick(self) -> None:
        now = time.time()
        if now >= self._next_touch:
            self._next_touch = now + 60
            await asyncio.to_thread(self.marker.touch)
        if self.cfg.brief.enabled:
            day = brief_due(self.cfg.brief.at, now, self.brief_sent)
            if day:
                await self.send_brief(day)
        if self.cfg.guidance.enabled:
            day = brief_due(self.cfg.guidance.at, now, self.guidance_sent, until_hour=23)
            if day:
                self.guidance_sent = day
                await self.guidance_review()
        if self.cfg.ari_health.enabled:
            day = brief_due(self.cfg.ari_health.at, now, self.health_sent, until_hour=23)
            if day:
                self.health_sent = day
                asyncio.create_task(self.run_health(push=True))  # takes a while (it asks the model): not in the tick
        if self.cfg.summary.enabled:
            day = brief_due(self.cfg.summary.at, now, self.summary_sent, until_hour=23)
            if day:
                await self.send_summary(day)

    async def run_health(self, push: bool = False) -> dict:
        """Ari's health check (health.py), kept as an event; the phone hears only when something is bad."""
        from . import health

        workers = await self.registry.workers()
        gpu = sum(1 for w in workers if w["state"] == "online" and "gpu" in w["capabilities"])
        pc = await self._health_on_pc(workers) if self.cfg.models.needs else None
        result = await asyncio.to_thread(health.run, self.cfg, tools=self.health_tools(),
                                         listener_seen=self.listener_seen(), gpu_workers=gpu, pc=pc,
                                         listener_info=self.listener_info())

        def fn(conn):
            now = time.time()
            insert_event(conn, now, "ari.health", src="argus", data=result)
            if push and not result["ok"]:
                day = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
                add_message(conn, now, "phone", phone_message("Ari needs a look", health.summary(result),
                                                            priority="low", tags=["stethoscope"]),
                            dedupe_key=f"health:{day}")

        await self.store.write(fn)
        if push:
            self.outbox.poke()
        return result

    async def _health_on_pc(self, workers: list[dict], wait: float = 90) -> list[dict]:
        """Argus on the laptop: the PC's part of Ari's health check (Ollama, SearXNG, voice, Whisper live there and
        answer on its 127.0.0.1) runs as a job on the PC's worker. The PC is never woken for it."""
        from . import health

        want = set(self.cfg.models.needs)
        if not any(w["state"] == "online" and want <= set(w["capabilities"]) for w in workers):
            return [health.PC_OFF]
        job_id, _ = await self.jobs.enqueue("ari", "health", {"tools": self.health_tools()},
                                            needs=self.model_needs(), priority=PRIORITY_INTERACTIVE,
                                            max_attempts=1, source="argus")
        end = time.monotonic() + wait
        while time.monotonic() < end:
            j = await self.jobs.get(job_id)
            if j.state.value == "succeeded":
                return list((j.result or {}).get("checks") or [])
            if j.state.value in ("dead", "cancelled"):
                return [health.row("the PC", "warn", f"its check failed: {(j.error or '?').splitlines()[0][:120]}",
                                   "see the PC's logs\\worker.log")]
            await asyncio.sleep(0.5)
        with contextlib.suppress(Exception):
            await self.jobs.cancel(job_id, "the PC took too long")  # don't leave it waiting to wake the PC later
        return [health.row("the PC", "warn", "didn't finish its checks in time", "see the PC's logs\\worker.log")]

    async def guidance_review(self) -> dict:
        """Queue the review of new mistakes (a job for a worker with Claude). Nothing to do: no job."""
        from . import guidance

        def fn(conn):
            now = time.time()
            items = guidance.review_inputs(conn, self.cfg.guidance.max_per_review)
            if not items:
                return None
            guidance.mark_reviewed(conn, [s["id"] for it in items for s in it["mistakes"]], now)
            jid, _ = self.jobs.enqueue_in(conn, now, "guidance", "review", {"playbooks": items},
                                          needs=self.model_needs(),
                                          priority=PRIORITY_BATCH, model_group="cloud", source="argus",
                                          dedupe_key="guidance:review")
            return {"job_id": jid, "playbooks": len(items)}

        return await self.store.write(fn) or {"job_id": None, "playbooks": 0, "note": "nothing new to learn from"}

    async def queue_evals(self, key: str) -> dict:
        """Replay a playbook's eval set now (job guidance.evals on the first local tier)."""
        from . import guidance

        def fn(conn):
            pb = guidance.playbook_input(conn, key)
            if pb is None:
                return None
            if not pb["evals"]:
                return {"job_id": None, "note": "no tests yet: mark some answers Correct first"}
            jid, _ = self.jobs.enqueue_in(conn, time.time(), "guidance", "evals", {"playbook": pb},
                                          needs=self.model_needs(),
                                          priority=PRIORITY_INTERACTIVE, source="helios",
                                          dedupe_key=f"guidance:evals:{key}")
            return {"job_id": jid, "tests": len(pb["evals"])}

        return await self.store.write(fn)

    async def queue_replay(self, sample_id: int, tier: str) -> dict | None:
        """Ask another model tier the same question as a kept answer (job guidance.replay)."""
        from . import guidance

        def fn(conn):
            s = conn.execute("SELECT * FROM samples WHERE id = ?", (sample_id,)).fetchone()
            if s is None:
                return None
            pb = guidance.playbook_input(conn, s["playbook"])
            inp = {"sample": guidance.sample_json(s), "tier": tier, "playbook": pb["playbook"],
                   "schema": pb["schema"], "lessons": pb["lessons"]}
            jid, _ = self.jobs.enqueue_in(conn, time.time(), "guidance", "replay", inp, needs=self.model_needs(),
                                          priority=PRIORITY_INTERACTIVE, source="helios",
                                          dedupe_key=f"guidance:replay:{sample_id}:{tier}")
            return {"job_id": jid}

        return await self.store.write(fn)

    async def send_summary(self, day: str | None = None) -> dict:
        """The evening summary to the phone (once per day; `POST /summary` sends one now)."""
        def fn(conn):
            now = time.time()
            title, text = compose_summary(conn, now, self.button_labels())
            key = f"summary:{day}" if day else f"summary:now:{int(now)}"
            oid = add_message(conn, now, "phone", phone_message(title, text, priority="low", tags=["crescent_moon"]),
                              dedupe_key=key)
            return {"title": title, "text": text, "queued": oid is not None}

        out = await self.store.write(fn)
        if day:
            self.summary_sent = day
        self.outbox.poke()
        return out

    def button_labels(self) -> dict[str, str]:
        from .ask import catalog

        try:
            return {a["id"]: a["label"] for a in catalog(self.plugin_host)}
        except Exception:
            return {}

    async def weather_line(self) -> str:
        """Today's weather in one line for the brief, or "" (no place set, unknown place, or no connection)."""
        place = self.cfg.brief.weather.strip()
        if not place:
            return ""
        try:
            w = await asyncio.wait_for(asyncio.to_thread(self.weather.today, place), 15)
        except Exception as e:  # the brief goes without it
            log.warning("no weather for the brief", extra={"place": place, "error": str(e)[:200]})
            return ""
        if w is None:
            log.warning("no such place for the weather", extra={"place": place})
            return ""
        return words(w)

    async def weather_say(self, place: str = "", offset: int = 0) -> str | None:
        """Ari's spoken weather for a place (default: brief.weather), today or tomorrow; None when it can't."""
        place = place.strip() or self.cfg.brief.weather.strip()
        if not place:
            return None
        try:
            w = await asyncio.wait_for(asyncio.to_thread(self.weather.day, place, offset), 15)
        except Exception as e:  # no connection: let the model try
            log.warning("no weather", extra={"place": place, "error": str(e)[:200]})
            return None
        if w is None:
            return f"I couldn't find a place called {place}."
        return weather_spoken(w, "tomorrow" if offset else "today")

    async def _brief_inputs(self) -> dict:
        needs = set(self.cfg.power.pc_needs)
        pc = any(w["state"] == "online" and set(w["capabilities"]) & needs for w in await self.registry.workers())
        return {"last_backup": self.last_backup, "pc_online": pc, "claude_cap": self.cfg.claude.calls_per_day,
                "weather": await self.weather_line()}

    async def send_brief(self, day: str | None = None) -> dict:
        """The morning brief to the phone (once per day; `POST /brief` sends one now)."""
        inputs = await self._brief_inputs()

        def fn(conn):
            now = time.time()
            title, text = compose_brief(conn, now, health=last_health(conn), **inputs)
            key = f"brief:{day}" if day else f"brief:now:{int(now)}"
            oid = add_message(conn, now, "phone", phone_message(title, text, tags=["sunrise"]), dedupe_key=key)
            return {"title": title, "text": text, "queued": oid is not None}

        out = await self.store.write(fn)
        if day:
            self.brief_sent = day
        self.outbox.poke()
        return out

    async def spoken_brief(self) -> str:
        """The brief as Ari says it ("good morning")."""
        inputs = await self._brief_inputs()
        now = time.time()
        parts = await self.store.read(lambda c: brief_parts(c, now, health=last_health(c), **inputs))
        return say_brief(parts, now)

    def _backup_due(self, now: float) -> bool:
        """Tonight's slot has passed and the newest backup is older than it."""
        if not self.cfg.backup.enabled:
            return False
        h, m = map(int, self.cfg.backup.at.split(":"))
        slot = datetime.fromtimestamp(now).replace(hour=h, minute=m, second=0, microsecond=0).timestamp()
        if now < slot:
            slot -= 86400
        if self.last_backup and self.last_backup.get("at", 0) >= slot:
            return False  # tried for this slot already (a failed one is not retried until the next)
        latest = self.backups.list()
        return not latest or latest[0]["made_at"] < slot

    async def backup_tick(self) -> None:
        if self._backup_due(time.time()):
            await self.backup_now()

    async def backup_now(self) -> dict:
        """Back up and check it; tell the phone if it failed; have the PC fetch a copy (backup.copy_to)."""
        try:
            res = await asyncio.to_thread(self.backups.make)
        except Exception as e:  # disk full, database locked for too long, ...
            res = {"ok": False, "problem": f"{type(e).__name__}: {e}"}
        self.last_backup = {**res, "at": time.time()}
        kind = "backup.made" if res["ok"] else "backup.failed"

        def fn(conn):
            now = time.time()
            insert_event(conn, now, kind, src="argus", dst="backup", data=res)
            if not res["ok"]:
                add_message(conn, now, "phone", phone_message("Argus backup failed", str(res.get("problem"))[:300],
                                                            priority="high", tags=["warning"]),
                            dedupe_key=f"backup-failed:{time.strftime('%Y%m%d', time.localtime(now))}")

        await self.store.write(fn)
        if not res["ok"]:
            self.outbox.poke()
        elif self.cfg.backup.copy_to:
            await self.jobs.enqueue("backup", "copy", {"file": res["file"], "to": self.cfg.backup.copy_to,
                                                       "keep": self.cfg.backup.keep},
                                    needs=["desktop"], priority=20, dedupe_key=f"backup:{res['file']}",
                                    source="argus")
        return res

    async def notify_from_job(self, job_id: str, worker: str, key: str, message: dict) -> bool:
        """ctx.notify(): queue a phone message for a job the worker holds. The key makes a retried step's
        message a no-op. Returns True if it was queued now."""

        quiet = self.cfg.notify.quiet and self.cfg.ntfy.quiet and self.cfg.summary.enabled and \
            int(message.get("priority") or 3) <= 3  # 1 min, 2 low, 3 default, 4 high, 5 urgent

        def fn(conn) -> bool:
            now = time.time()
            job = self.jobs._get(conn, job_id)
            self.jobs._check_lease(job, worker, now)
            if quiet:  # waits for the evening summary
                cur = conn.execute("INSERT OR IGNORE INTO held_notes (job_id, plugin, title, text, dedupe_key,"
                                   " created_at) VALUES (?,?,?,?,?,?)",
                                   (job_id, job.plugin, str(message.get("title") or "")[:200],
                                    str(message.get("message") or "")[:1000], f"notify:{key}", now))
                if cur.rowcount:
                    insert_event(conn, now, "notify.held", job_id=job_id, src=job.plugin, dst="argus",
                                 data={"title": message.get("title")})
                return False
            oid = add_message(conn, now, "phone", message, dedupe_key=f"notify:{key}", job_id=job_id)
            if oid is not None:
                insert_event(conn, now, "notify.queued", job_id=job_id, src=job.plugin, dst="argus",
                             data={"title": message.get("title")})
            return oid is not None

        queued = await self.store.write(fn)
        if queued:
            self.outbox.poke()
        return queued

    async def apply_plugin_settings(self) -> None:
        """Your changes from Helios (live, settings) on top of argus.yaml."""
        import json

        def fn(conn):
            return {r[0]: json.loads(r[1]) for r in conn.execute(
                "SELECT plugin, value FROM plugin_state WHERE key = '_settings' AND value IS NOT NULL")}

        saved = await self.store.read(fn)
        for pid, p in self.plugin_host.plugins.items():
            p.apply(saved.get(pid))

    async def register_plugins(self) -> None:
        from .registry import _upsert_component

        def fn(conn) -> None:
            now = time.time()
            for pid, p in self.plugin_host.plugins.items():
                node = p.manifest.helios.node
                _upsert_component(conn, now, pid, "plugin", node.label or p.manifest.name, node.group,
                                  {"version": p.manifest.version, "live": p.live, "kind": p.manifest.kind})

        await self.store.write(fn)

    def path_rules(self) -> dict[str, list[str]]:
        """Folders any plugin may touch at all (argus.yaml paths), on top of each manifest's own list."""
        return {"allowed": [str(p) for p in self.cfg.paths.allowed],
                "blocked": [str(p) for p in self.cfg.paths.blocked]}

    async def approval_tick(self) -> None:
        done = await self.approvals.tick()
        if done["reminded"] or done["expired"]:
            self.outbox.poke()

    async def prune_events(self) -> int:
        """Delete events past the retention period, a chunk per watchdog tick, until caught up; then rest
        for an hour."""
        now = time.time()
        if now < self._next_prune:
            return 0
        cutoff = now - self.cfg.events.retention_days * 86400
        n = await self.store.write(lambda conn: prune_events(conn, cutoff))
        self.pruned_events += n
        if n == 0:
            self._next_prune = now + 3600
        else:
            log.info("old events pruned", extra={"count": n})
        return n

    async def stop(self) -> None:
        await self.watchdog.stop()
        await asyncio.to_thread(self.marker.stop)  # a clean stop: no "Argus is back" message next time
        await self.phone.stop()
        await self.outbox.stop()
        await self.hub.stop()
        self.store.close()
        log.info("argus stopped")

    def health(self) -> dict:
        db = self.store.health()
        ok = db["ok"] and self.watchdog.alive and self.hub.alive and self.outbox.alive
        ok = ok and self.phone.alive
        return {
            "status": "ok" if ok else "degraded",
            "version": self.version,
            "instance": self.cfg.instance.name,
            "host": self.cfg.instance.host,
            "uptime_seconds": round(time.time() - self.started_at, 1) if self.started_at else 0,
            "database": db,
            "events": self.hub.stats(),
            "outbox": self.outbox.health(),
            "phone": self.phone.health(),
            "watchdog": {
                "alive": self.watchdog.alive,
                "runs": self.watchdog.runs,
                "last_error": self.watchdog.last_error,
            },
        }
