# Controls: stop, start, restart and pause

Helios can stop, start and restart plugins and workers, pause everything, and read a component's log. Everything
here is behind the Argus token, JSON, and survives an Argus restart (it lives in the database: migration 0020).
**The core has no control.** Argus itself (id `argus`, `argusd`, `core`) is refused with 403 on every route below,
and "pause everything" never touches the API, Helios, the backups or approvals.

## Plugins

| Route | What it does |
| --- | --- |
| `POST /plugins/{id}/stop` | Pause the plugin (optional body `{"reason": "..."}`). Its queued jobs stay queued and are not handed to a worker; the job a worker is running finishes. Nothing new is queued for it: schedules skip their slot, folder triggers and webhooks answer 409 (the file or the sender tries again), and a job made through the API gets `409 {"error": "plugin_paused"}`. Ari's tools and buttons say it is stopped. |
| `POST /plugins/{id}/start` | Clear the pause. The queue carries on where it stopped. |
| `POST /plugins/{id}/restart` | Start, put the plugin's dead or expired leases back in the queue (a lease is dead when its worker is offline, expired when it ran out), and raise the plugin's `generation`. A worker that sees a new generation (in `plugin_info.generation` of the job it claims) loads the plugin's code again before running it. If the new code does not import, the old code keeps running and the error is kept. Answers `{plugin, paused, requeued, generation}`. |

`plugins.off` in `argus.yaml` stays what it was: such a plugin is not loaded, and `start`, `stop` and `restart`
answer `409` with the reason. An unknown plugin is `404`.

## Workers

| Route | What it does |
| --- | --- |
| `POST /workers/{id}/pause` | The worker's claims return no job. It keeps heartbeating (its claim poll is the heartbeat) and finishes the job it has. |
| `POST /workers/{id}/resume` | Claims work again. |
| `POST /workers/{id}/restart` | Flag the worker. On its next claim it is told (`204` with the header `X-Argus-Control: restart`), finishes the job it has, and exits with code **75**. Nothing is ever killed in the middle of a job. `409` when the worker is offline. |

A worker is one box on the map: its fast lane (`<id>-now`) and the PC's session twin (`desktop-<host>`) count with it,
so pausing or restarting the box covers them, and `/workers/worker-pc-now/pause` pauses `worker-pc`. A restart asks
each process of the box; each exits after its own job (the fast lane waits for the main lane and the other way round).

### Exit and restart: what brings the worker back

Code 75 is `argus.exitcodes.EXIT_RESTART`, the one the supervisor already uses for "start me again now".

- **Windows (the PC):** `argus.supervisor` starts the worker and, on exit code 75, starts it again at once (not counted as a crash, no waiting).
- **Linux (the laptop):** `deploy/linux/argus-worker.service` has `Restart=always`, so systemd starts it again after `RestartSec` (10 s); `SuccessExitStatus=75` keeps it from being logged as a failure.
- **A worker run by hand** (`dev.ps1 worker`, a terminal) has nothing to start it again, so it **ignores** the restart request and logs a warning. The supervisor and the unit set `ARGUS_SUPERVISED=1`, and systemd sets `INVOCATION_ID` for every service (so a unit file installed before this change still counts); that is how the worker knows. The Windows supervisor passes the variable once it has been restarted on this code; a worker it starts before that logs the warning and ignores the request.
- The phone app's worker has no process to restart; it pauses and resumes like the others and ignores the restart.

A worker started before this change does not know the header and simply sees "no job".

## Everything at once

| Route | What it does |
| --- | --- |
| `GET /control/state` | `{"plugins_paused": [...], "workers_paused": [...], "paused_all": false}` |
| `POST /control/pause-all` | Pause every plugin and every worker that is not paused already, and remember that set. Answers what it added. The core API, Helios, the backups and approvals stay up; with every worker paused, nothing runs (the built-in jobs too) until you resume. |
| `POST /control/resume-all` | Put back exactly what pause-all paused. What you paused yourself stays paused, and so does anything you started or stopped by hand after pause-all (it became yours). |

A worker that registers after pause-all (a new id) is not paused.

## The map

`GET /map` plugin and worker nodes carry `paused: bool` and, while paused, `since` (epoch seconds). The queue
(`GET /queue`) shows a stopped plugin's queued jobs with `why: "paused"`.

## Events

Every control writes an event with `src` = `helios` and `dst` = the component: `control.paused`, `control.resumed`,
`control.restarted` (pause-all and resume-all write one per component plus one to `argus` with the lists). They are
in the live stream and the audit. When a worker takes its restart it writes `worker.restarting`.

## Logs

`GET /logs/{component}?lines=200&level=all|error&since=<epoch>`, where the component is a plugin id, a worker id or
`argus`. It answers a JSON list, newest last, at most 500 (a larger `lines` is cut to 500):

```json
[{"ts": 1794650400.0, "level": "error", "source": "worker", "msg": "job failed (boom token=***)"}]
```

The lines come from the log files `read_log` reads (`logs/argus.log`, `worker.log`, `worker-laptop.log`, ...), kept when
they are about the component (the plugin's logger or `plugin` field, the worker's id, the id as a word; the whole file
for `argus` and for a log named like the worker), **plus** the component's recent events from the database (a job that
failed for good, a step that failed, a worker that went offline, a control from Helios; `source` is `events`), so a
stopped or offline component says why. `ts` is epoch seconds, `level` is `debug|info|warn|error`, `since` keeps lines
after that time and `level=error` only the errors.

Tokens, passwords, bearer headers, one-time link tokens, well-known key shapes, long unbroken secret-looking runs and
every secret value in the config are replaced by `***`; a line is cut at 400 characters. Unknown component: `404`.

**One route, two answers.** `/logs/{name}` was already Helios's Logs page (`{entries, offset}`, for the file `name`).
That stays when the call has neither `level` nor `since` and `name` is a log file. Send `level=all` (or `since`) to get
the component list, always from the live map; a name that is not a log file (a plugin or worker id) always gets it.
