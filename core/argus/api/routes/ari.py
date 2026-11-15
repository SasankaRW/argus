"""Ari: asking, listening, power, game mode, live state, health and the PC's popup."""

from __future__ import annotations

import contextlib
import json
import time
from collections import deque
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from ... import ask as ask_mod
from ... import settings as settings_mod
from ... import thread as thread_mod
from ... import vocab
from ...ari import ari as ari_mod
from ...config import PRIORITY_INTERACTIVE
from ...events import EventFilter, insert_event, read_events
from ...expressive import plain
from ...ids import new_id
from ...tools import ToolError
from ...worker import think as think_mod
from ..schemas import AriAnswer, AriListening, AriSay, AriStateIn, ConvScore
from ._deps import Deps

WAKING = "Waking the PC, about a minute"  # Ari's state while no worker with the models is up


def build_router(deps: Deps) -> APIRouter:
    argus = deps.argus
    router = APIRouter()
    guarded = deps.guarded
    tools = deps.tools

    def _schedule_spec(action: str) -> dict:
        if action.startswith("remind:"):
            return {"plugin": "argus", "workflow": "remind", "input": {"text": action[7:]}, "needs": [],
                    "priority": 50}
        if action.startswith("power:"):
            return {"plugin": "power", "workflow": action[6:],
                    "input": {"delay": argus.cfg.power.shutdown_delay_seconds}, "needs": ["desktop"], "priority": 100}
        _, pid, wf = (action.split(":", 2) + ["", ""])[:3]
        p = argus.plugin_host.plugins.get(pid)
        if p is None:
            raise HTTPException(status_code=404, detail=f"no plugin {pid}")
        return {"plugin": pid, "workflow": wf, "input": {}, "needs": p.manifest.job_needs(), "priority": 50}

    async def _answer(conv: str, q: dict, yes: bool) -> dict:
        """You said yes or no to what Ari asked."""
        await argus.store.write(lambda c: ari_mod.close_question(c, q["turn"]))
        if not yes:
            reply, out = "Okay, I won't.", {}
        elif q["kind"] == "tool":
            try:
                res = await tools.run_now(q["name"], q.get("args") or {})
                nxt = str(res.get("next") or "") if isinstance(res, dict) else ""
                reply = (think_mod.said_back(q["name"], q.get("args") or {}, res)
                         or (f"It's ready: {nxt}." if nxt else "Done."))  # "It's ready: press Enter to send."
                out = {"tool": q["name"], "result": res}
            except ToolError as e:
                reply, out = f"I couldn't: {e}.", {}
        elif q["kind"] == "remember":
            fact = str(q.get("fact") or "")[:500]
            await argus.store.write(lambda c: ari_mod.remember(c, fact))
            reply, out = "Got it, I'll remember that.", {"remembered": fact}
        elif q["kind"] == "schedule":
            when = ari_mod.When(q["cron"], q["once"], q["say"], "")
            spec = _schedule_spec(q["action"])
            label = f"{q['say']}: {q['label']}"
            s = await argus.store.write(lambda c: ari_mod.add_schedule(c, time.time(), when, q["action"], label=label,
                                                                       **spec))
            reply, out = f"Done. {q['say'][0].upper()}{q['say'][1:]}, I'll {q['label']}.", {"schedule": s}
        else:
            if q["action"].startswith(("power:", "laptop:")) and q["action"] != "power:wake":
                try:
                    out = await deps.do_action(q["action"])
                except HTTPException as e:
                    reply, out = f"I couldn't: {e.detail}.", {}
                    await argus.store.write(lambda c: ari_mod.add_turn(c, conv, "ari", reply))
                    return {"conv": conv, "reply": reply}
            else:
                out = await deps.do_action(q["action"])
            reply = f"Opening {out['view']}." if "view" in out else "Ringing it now." if "phone" in out else "Done."
        await argus.store.write(lambda c: (ari_mod.add_turn(c, conv, "ari", reply), ari_state(c, "done", reply)))
        return {"conv": conv, "reply": reply, **out}

    def ari_state(c, phase: str, text: str = "", job_id: str | None = None, by: str = "argus") -> None:
        """What Ari is doing (event ari.state): the Ari pill in Helios and the PC's popup follow it."""
        insert_event(c, time.time(), "ari.state", job_id=job_id, data={"phase": phase, "text": plain(text)[:300],
                                                                       "by": by})

    overlays: dict[str, float] = {}  # host -> last ping from the PC's Ari popup

    @router.post("/ari/state", dependencies=guarded)
    async def ari_state_set(body: AriStateIn) -> dict:
        """Report what Ari is doing where you are (listening, speaking) so every Ari pill shows it."""
        await argus.store.write(lambda c: ari_state(c, body.phase, plain(body.text), by=body.by or "client"))
        return {"ok": True}

    listener = {"seen": 0.0}
    argus.listener_seen = lambda: listener["seen"]
    argus.listener_info = lambda: {k: listener.get(k) for k in ("mic", "loudest")}
    argus.health_tools = lambda: [{**t.brief(), "plugin": t.plugin, "risky": t.risky}
                                  for t in tools.all().values() if t.for_ari]

    @router.get("/ari/health", dependencies=guarded)
    async def ari_health_last() -> dict:
        """The newest health check (Ari's setup: Ollama, SearXNG, the voice, Whisper, the listener), or null."""
        rows = await argus.store.read(lambda c: read_events(c, 0, limit=1, flt=EventFilter.parse("ari.health"),
                                                            newest=True))
        return {"health": rows[-1]["data"] if rows else None}

    @router.get("/ari/health/history", dependencies=guarded)
    async def ari_health_history(limit: int = 30) -> dict:
        """Tool-picking accuracy and model step time per check (oldest first), for the trend in Helios."""
        rows = await argus.store.read(lambda c: read_events(c, 0, limit=max(1, min(limit, 120)),
                                                            flt=EventFilter.parse("ari.health"), newest=True))
        out = []
        for r in rows:
            d = r["data"] or {}
            pick = next((c for c in d.get("checks", []) if c.get("name") == "tool picking" and c.get("data")), None)
            out.append({"at": d.get("at") or r.get("ts"), "ok": bool(d.get("ok")),
                        "problems": d.get("problems") or 0, **(pick["data"] if pick else {})})
        out.sort(key=lambda x: x["at"] or 0)
        return {"history": out}

    @router.post("/ari/conversations", dependencies=guarded)
    async def ari_conversations_keep(body: ConvScore) -> dict:
        """Keep a run of the scripted conversations (does Ari keep the thread?), for the trend in Helios."""
        data = body.model_dump()
        data["at"] = data["at"] or time.time()
        await argus.store.write(lambda c: insert_event(c, time.time(), "ari.conversations", src="ari-eval",
                                                       dst="helios", data=data))
        return {"ok": True}

    @router.get("/ari/conversations", dependencies=guarded)
    async def ari_conversations(limit: int = 30) -> dict:
        """The conversation runs, oldest first: conversations and steps right, the misses of each."""
        rows = await argus.store.read(lambda c: read_events(c, 0, limit=max(1, min(limit, 120)),
                                                            flt=EventFilter.parse("ari.conversations"), newest=True))
        out = [r["data"] or {} for r in rows]
        out.sort(key=lambda x: x.get("at") or 0)
        return {"runs": out}

    @router.post("/ari/health", dependencies=guarded)
    async def ari_health_run() -> dict:
        """Check now (takes a few seconds: it asks the model a few questions)."""
        return {"health": await argus.run_health(push=False)}
    hush = {"until": 0.0}  # Ari isn't listening (anywhere) until then; 0 = listening

    def listening_now() -> dict:
        until = hush["until"]
        paused = until > time.time()
        return {"listening": not paused, "until": until if paused and until < 1e12 else None}

    @router.get("/ari/listening", dependencies=guarded)
    async def ari_listening() -> dict:
        """Is Ari listening for "Hey Ari"? (paused while you train your voice, or with the pause button)"""
        return listening_now()

    @router.post("/ari/listening", dependencies=guarded)
    async def ari_listening_set(body: AriListening) -> dict:
        """Pause Ari's ears everywhere (the PC's microphone, Helios), for a while or until turned back on."""
        hush["until"] = 0.0 if body.on else (time.time() + body.minutes * 60 if body.minutes else 1e13)
        now = listening_now()
        await argus.store.write(lambda c: insert_event(c, time.time(), "ari.listening", src="helios", dst="ari",
                                                       data=now))
        return now

    @router.post("/ari/power", dependencies=guarded)
    async def ari_power(body: AriListening) -> dict:
        """Ari off: no listening anywhere, quiet at once, answers on their way dropped (the island, Helios, or
        "Ari, turn off"). On: back to normal. Stays off until turned back on (or for `minutes`)."""
        return await set_power(body.on, body.minutes)

    async def set_power(on: bool, minutes: float | None = None) -> dict:
        hush["until"] = 0.0 if on else (time.time() + minutes * 60 if minutes else 1e13)
        now = listening_now()

        def fn(c):
            dropped = []
            if not on:
                for (jid,) in c.execute("SELECT id FROM jobs WHERE plugin = 'ari' AND workflow = 'think'"
                                        " AND state IN ('queued', 'retry', 'leased', 'running')").fetchall():
                    with contextlib.suppress(Exception):  # finished meanwhile
                        argus.jobs.cancel_in(c, jid, "Ari was turned off")
                        dropped.append(jid)
                insert_event(c, time.time(), "ari.stop", src="helios", dst="ari", data={"job": "", "off": True})
            insert_event(c, time.time(), "ari.listening", src="helios", dst="ari", data=now)
            ari_state(c, "idle" if on else "off", "" if on else "Ari is off", by="argus")
            return dropped

        dropped = await argus.store.write(fn)
        return {**now, "on": on, "dropped": len(dropped)}


    live_now: dict[str, Any] = {"model": None, "model_at": 0.0,
                                "voice": {"mic": deque(maxlen=200), "out": deque(maxlen=200), "speaking": False,
                                          "english": None, "at": 0.0}}

    @router.post("/models/live", dependencies=guarded)
    async def models_live(body: dict[str, Any]) -> dict:
        """A model answering right now (from a worker): {state: start | text | end, job_id, step, model, tier,
        text so far, ms, verdict, tokens}. Only the newest is kept, in memory."""
        keep = {k: body.get(k) for k in ("state", "job_id", "step", "model", "tier", "attempt", "prompt_chars",
                                         "text", "ms", "verdict", "why", "tokens_in", "tokens_out", "worker")}
        if isinstance(keep.get("text"), str):
            keep["text"] = keep["text"][-600:]
        prev = live_now["model"] or {}
        if keep.get("state") == "text" and prev.get("state") == "end" and prev.get("job_id") == keep.get("job_id") \
                and prev.get("step") == keep.get("step") and prev.get("attempt") == keep.get("attempt"):
            return {"ok": True}  # a late piece of an answer already finished
        live_now["model"], live_now["model_at"] = keep, time.time()
        return {"ok": True}

    @router.post("/ari/live", dependencies=guarded)
    async def ari_live(body: dict[str, Any]) -> dict:
        """The PC's listener, a few times a second while something happens: the mic's levels and Ari's voice's
        (0..1 each), whether Ari is speaking, and how English the last thing heard was."""
        v = live_now["voice"]
        for k in ("mic", "out"):
            v[k].extend(max(0.0, min(1.0, float(x))) for x in (body.get(k) or [])[:60]
                        if isinstance(x, (int, float)))
        v["speaking"] = bool(body.get("speaking"))
        if body.get("english") is not None:
            v["english"] = body.get("english")
        v["at"] = time.time()
        return {"ok": True}

    @router.get("/live", dependencies=guarded)
    async def live_get() -> dict:
        """What's happening this second: the voice levels (newest last) and the model answering now."""
        v = live_now["voice"]
        return {"now": time.time(), "voice": {"mic": list(v["mic"])[-120:], "out": list(v["out"])[-120:],
                                              "speaking": v["speaking"], "english": v["english"], "at": v["at"]},
                "model": live_now["model"], "model_at": live_now["model_at"]}


    game: dict[str, Any] = {"on": False, "by": None, "since": None, "app": "", "freed_mb": None, "unloaded": [],
                            "still": [], "used_mb": None, "ari_was_off": False}

    def game_now() -> dict:
        return {"on": game["on"], "auto": bool(argus.cfg.ari.game_mode_auto), "by": game["by"],
                "since": game["since"], "app": game["app"], "freed_mb": game["freed_mb"],
                "unloaded": game["unloaded"], "still": game["still"], "used_mb": game["used_mb"]}

    @router.get("/ari/game", dependencies=guarded)
    async def ari_game() -> dict:
        """Game mode: {on, auto, by: you | auto, since, app, freed_mb}."""
        return game_now()

    @router.post("/ari/game", dependencies=guarded)
    async def ari_game_set(body: dict[str, Any]) -> dict:
        """{on, by?, app?}: game mode on or off (by "auto" from the PC; an automatic off never ends one you turned
        on). {auto}: turn the automatic part on or off (kept). {freed_mb, unloaded}: what the PC freed;
        {still, used_mb}: what else still uses its GPU."""
        return await set_game(body)

    async def set_game(body: dict[str, Any]) -> dict:
        if "auto" in body:
            try:
                await argus.change_settings({"ari.game_mode_auto": bool(body["auto"])})
            except settings_mod.SettingError as e:
                raise HTTPException(status_code=422, detail=str(e)) from None
        if "freed_mb" in body or "unloaded" in body:
            game["freed_mb"] = body.get("freed_mb")
            game["unloaded"] = [str(x) for x in body.get("unloaded") or []][:20]
        if "still" in body:  # what else still uses the GPU, and how much of it is in use
            game["still"] = [str(x)[:60] for x in body.get("still") or []][:20]
            game["used_mb"] = body.get("used_mb")
        by = "auto" if body.get("by") == "auto" else "you"
        if "on" in body and bool(body["on"]) != game["on"]:
            if body["on"]:
                game.update(on=True, by=by, since=time.time(), app=str(body.get("app") or "")[:60], freed_mb=None,
                            unloaded=[], still=[], used_mb=None, ari_was_off=hush["until"] > time.time())
                argus.models.local_paused = "game mode: the PC's GPU is for the game"
                await set_power(False)
            elif by == "you" or game["by"] == "auto":  # an automatic off never ends game mode you turned on
                game.update(on=False, by=by, since=time.time(), app="")
                argus.models.local_paused = None
                if not game["ari_was_off"]:
                    await set_power(True)
        now = game_now()
        await argus.store.write(lambda c: insert_event(c, time.time(), "ari.game", src="helios", dst="ari", data=now))
        return now

    @router.post("/ari/listener", dependencies=guarded)
    async def ari_listener_here(body: dict | None = None) -> dict:
        """The PC's "Hey Ari" listener says it is running (every 30 s), with its mic and how loud it heard."""
        listener["seen"] = time.time()
        for k in ("mic", "loudest"):
            if body and body.get(k) is not None:
                listener[k] = body[k]
        if body and "busy" in body:  # in a call or a game (None: neither): Ari keeps it to one quiet line
            listener["busy"] = body.get("busy")
        return {"ok": True}

    @router.get("/ari/vocabulary", dependencies=guarded)
    async def vocabulary() -> dict:
        """The words Whisper should expect (ari.vocabulary + names Ari remembers) and the fixes (ari.heard_as)."""
        facts = await argus.store.read(lambda c: [m["fact"] for m in ari_mod.memories(c)])
        names = vocab.words(argus.cfg.ari.vocabulary, facts)
        return {"words": names, "prompt": vocab.prompt(names), "heard_as": argus.cfg.ari.heard_as,
                "whisper_model": argus.cfg.ari.whisper_model}

    @router.get("/ari/listener", dependencies=guarded)
    async def ari_listener_running() -> dict:
        """Is the PC's "Hey Ari" listener running? (Helios on that PC then doesn't listen for "Hey Ari" too.)"""
        return {"listener": time.time() - listener["seen"] < 75}

    @router.post("/ari/wake", dependencies=guarded)
    async def ari_wake() -> dict:
        """The island's Talk button: the PC's listener starts listening now, no "Hey Ari" needed. {listener: false}
        when no listener runs (the island then opens Helios's mic instead)."""
        here = time.time() - listener["seen"] < 75
        if here:
            await argus.store.write(lambda c: insert_event(c, time.time(), "ari.wake", src="island", dst="ari"))
        return {"listener": here}

    @router.post("/ari/stop", dependencies=guarded)
    async def ari_stop(body: dict | None = None) -> dict:
        """The island's Stop button: Ari stops talking and drops the answer it is working on (the PC's listener
        hears ari.stop; the job, when given, is cancelled)."""
        job = str((body or {}).get("job") or "")
        stopped = False
        if job:
            with contextlib.suppress(Exception):  # already finished or unknown: nothing to cancel
                await argus.jobs.cancel(job, "stopped from the island")
                stopped = True
        await argus.store.write(lambda c: (insert_event(c, time.time(), "ari.stop", src="island", dst="ari",
                                                        data={"job": job}),
                                           ari_state(c, "idle", "", by="argus")))
        return {"ok": True, "cancelled": stopped}

    @router.post("/ari/popup", dependencies=guarded)
    async def ari_popup_ping(request: Request) -> dict:
        """The PC's Ari popup is running (sent every 30 s): Helios on that PC then leaves the pill to it."""
        overlays[request.client.host if request.client else "?"] = time.time()
        return {"ok": True}

    @router.post("/ari", dependencies=guarded)
    async def ari_say(body: AriSay) -> dict:
        """Say something to Ari. Returns the reply at once, or a turn that fills in (poll GET /ari/{conv})."""
        out = await ari_say_inner(body)
        conv = out.get("conv") or body.conv
        if conv and (body.source or body.heard):  # how it came, for the timeline
            meta = json.dumps({"source": body.source or "typed", "heard": body.heard or None}, default=str)[:2000]
            await argus.store.write(lambda c: c.execute(
                "UPDATE ari_turns SET meta = ? WHERE id = (SELECT id FROM ari_turns WHERE conv = ? AND role = 'you'"
                " ORDER BY id DESC LIMIT 1)", (meta, conv)))
        return out

    async def ari_say_inner(body: AriSay) -> dict:
        conv = body.conv or new_id().lower()
        body.text = vocab.fix(body.text, argus.cfg.ari.heard_as)  # "open what's up" -> "open WhatsApp"
        text = ari_mod.WAKE.sub("", body.text).strip() or body.text.strip()
        if ari_mod.TURN_OFF.match(text):  # "Ari, turn off": off until the island's or Helios's Start
            reply = "Okay, I'm off. Turn me back on from the island or Helios."
            await argus.store.write(lambda c: (ari_mod.add_turn(c, conv, "you", body.text),
                                               ari_mod.add_turn(c, conv, "ari", reply)))
            await set_power(False)
            return {"conv": conv, "reply": reply}
        q = await argus.store.read(lambda c: ari_mod.open_question(c, conv))
        if q and not await argus.store.read(lambda c: ari_mod.open_question(c, conv, max_age=600)):
            await argus.store.write(lambda c: ari_mod.close_question(c, q["turn"]))  # asked too long ago: lapsed
            q = None
        if not q and ari_mod.CORRECTION.match(text):  # "no, I meant ...": the last answer's model call was wrong
            await argus.store.write(lambda c: ari_mod.mark_corrected(c, conv, text, time.time()))
        await argus.store.write(lambda c: ari_mod.add_turn(c, conv, "you", body.text.strip()))
        if q and (ari_mod.YES.match(text) or ari_mod.NO.match(text)):
            return await _answer(conv, q, bool(ari_mod.YES.match(text)))
        if q:  # asked something else instead: the question lapses
            await argus.store.write(lambda c: ari_mod.close_question(c, q["turn"]))

        async def reply(said: str, action: str | None = None, pending: dict | None = None,
                        used: list[dict] | None = None) -> dict:
            def keep(c) -> int:  # the answer, and the thread after it (as Ari's thinking would have left it)
                after = thread_mod.update(ari_mod.thread(c, conv), message=text,
                                          out={"reply": said, "used": used or [], "pending": pending})
                tid = ari_mod.add_turn(c, conv, "ari", said, action=action, pending=pending)
                c.execute("UPDATE ari_turns SET meta = ? WHERE id = ?", (json.dumps({"thread": after}), tid))
                ari_state(c, "done", said)
                return tid

            tid = await argus.store.write(keep)
            return {"conv": conv, "turn": tid, "reply": said, "action": action, "pending": pending}

        if ari_mod.BRIEF.match(text):  # "good morning": the morning brief, spoken
            return await reply(await argus.spoken_brief())
        wx = ari_mod.weather_ask(text)
        if wx is not None:  # "how's the weather?", "will it rain tomorrow in Kandy?": Open-Meteo, no model
            said = await argus.weather_say(*wx)
            if said:
                return await reply(said, used=[{"tool": "weather", "args": {
                    **({"place": wx[0]} if wx[0] else {}), **({"tomorrow": True} if wx[1] else {})}}])
        pref = ari_mod.preference_of(text)
        if pref:  # "keep it shorter", "call me Sas": how to talk to you, kept after your yes
            said = pref[len(ari_mod.STYLE):]
            return await reply(f"Sure: {said}. Want me to keep that from now on?", None,
                               {"kind": "remember", "fact": pref})
        actions = ask_mod.catalog(argus.plugin_host)
        m = ari_mod.REMEMBER.match(text)
        if m and not ari_mod.parse_when(text, time.time()):  # "remember that ..." (not "remind me at ...")
            await argus.store.write(lambda c: ari_mod.remember(c, m.group("fact")))
            return await reply("Got it, I'll remember that.")
        when = ari_mod.parse_when(text, time.time())
        if when is not None:
            what = ari_mod.action_for(when.rest, actions)
            if what is None:
                return await reply(f"I got the time ({when.say}) but not what to do. Say it like \"sort downloads "
                                   f"every morning at 7\" or \"remind me to call mum tomorrow at 5 pm\".")
            action = what[0]
            label = ari_mod.label_of(action, actions)
            return await reply(f"{when.say[0].upper()}{when.say[1:]}, I'll {label}. Shall I set that up?",
                               pending={"kind": "schedule", "action": action, "cron": when.cron, "once": when.once,
                                        "say": when.say, "label": label})
        if ask_mod.PHONE.search(text.lower()):  # "where's my phone?": where Tailscale sees it, and ring it?
            where = await deps.phone_where()
            return await reply(f"{where['text']} Want me to ring it?", "phone:ring",
                               {"kind": "action", "action": "phone:ring"})
        snap = await ask_mod.snapshot(argus)
        # about the screen or the clipboard ("what does this error say?"): not Argus's own errors
        about_screen = think_mod.SCREEN.search(text) or think_mod.CLIPBOARD.search(text)
        hit = None if about_screen else ask_mod.rules(text, actions, snap)
        if hit is not None:
            act = hit.get("action")
            if act and not act.startswith("show:"):  # something to do: wait for your yes
                return await reply(hit["reply"], act, {"kind": "action", "action": act})
            return await reply(hit["reply"], act)
        hist = await argus.store.read(lambda c: ari_mod.history(c, conv))
        known = await argus.store.read(lambda c: ari_mod.recall(c, text))
        prefs = await argus.store.read(ari_mod.preferences)
        thread = await argus.store.read(lambda c: ari_mod.thread(c, conv))
        now_t = time.time()
        earlier = await argus.store.read(lambda c: ari_mod.earlier_today(c, conv, now_t))
        before = await argus.store.read(lambda c: ari_mod.episodes_like(c, text, now_t, not_conv=conv))
        job_id, _ = await argus.jobs.enqueue(
            "ari", "think", {"text": text, "history": hist[:-1], "now": time.strftime("%A %d %B %Y, %H:%M"),
                             "thread": thread, "earlier_today": earlier, "before": before,
                             "you_remember": known, "their_preferences": prefs,
                             "busy": listener.get("busy") if time.time() - listener["seen"] < 75 else None,
                             "personality": argus.cfg.ari.personality, "call_me": argus.cfg.ari.call_me,
                             "plan_with_claude": argus.cfg.ari.plan_with_claude,
                             "tools": [t.brief() for t in tools.all().values() if t.for_ari]},
            needs=list(argus.cfg.models.needs), priority=PRIORITY_INTERACTIVE, source="helios",
            max_attempts=1)  # a reply that failed is said to have failed; trying again minutes later helps no one
        await deps.remember_in_background(conv)
        # no worker with the models is up (the PC is off): the power manager wakes it; say so instead of hanging
        waking = not await deps.able(argus.cfg.models.needs)
        tid = await argus.store.write(lambda c: (ari_mod.add_turn(c, conv, "ari", None, job_id=job_id),
                                                 ari_state(c, "thinking", WAKING if waking else text, job_id))[0])
        return {"conv": conv, "turn": tid, "job_id": job_id, "reply": None}

    @router.post("/ari/{conv}/answer", dependencies=guarded)
    async def ari_answer(conv: str, body: AriAnswer) -> dict:
        """The Yes / No buttons under Ari's question."""
        q = await argus.store.read(lambda c: ari_mod.open_question(c, conv))
        if q is None:
            raise HTTPException(status_code=409, detail="nothing to answer")
        await argus.store.write(lambda c: ari_mod.add_turn(c, conv, "you", "Yes" if body.yes else "No"))
        return await _answer(conv, q, body.yes)

    deps.set_game = set_game
    deps.overlays = overlays
    deps.vocabulary = vocabulary
    return router
