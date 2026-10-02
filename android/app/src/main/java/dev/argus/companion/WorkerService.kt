package dev.argus.companion

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.net.ConnectivityManager
import android.net.Network
import android.os.Build
import android.os.IBinder
import android.util.Log
import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import kotlin.concurrent.thread

/**
 * Keeps the phone connected to Argus, with two loops that only make outgoing requests to your Argus (nothing
 * listens, nothing goes through an outside service):
 *  - the inbox: asks for notifications (waiting up to 25 s), shows them, acknowledges them, asks again, so they
 *    arrive with the app closed;
 *  - the worker (when "let Argus use this phone" is on): capability "phone", runs the phone plugin's jobs.
 */
class WorkerService : Service() {

    companion object {
        const val CHANNEL_ON = "argus-on"
        const val CHANNEL_ASK = "argus-ask"
        private const val TAG = "argus-worker"

        fun start(ctx: Context) {
            if (!Prefs(ctx).ready) return
            ctx.startForegroundService(Intent(ctx, WorkerService::class.java))
        }

        fun stop(ctx: Context) = ctx.stopService(Intent(ctx, WorkerService::class.java))

        fun channels(ctx: Context) {
            val nm = ctx.getSystemService(NotificationManager::class.java)
            nm.createNotificationChannel(NotificationChannel(CHANNEL_ON, "Connected to Argus",
                NotificationManager.IMPORTANCE_MIN).apply { setShowBadge(false) })
            nm.createNotificationChannel(NotificationChannel(CHANNEL_ASK, "Ari asks",
                NotificationManager.IMPORTANCE_HIGH))
            Notifier.channels(ctx)
        }
    }

    @Volatile private var running = false
    private var loop: Thread? = null
    private var inboxLoop: Thread? = null
    private val workerPause = Pause()
    private val inboxPause = Pause()
    private var lastText = "connecting…"
    private var netCallback: ConnectivityManager.NetworkCallback? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        channels(this)
        show(lastText)  // Android wants startForeground after every startForegroundService
        if (!running) {
            running = true
            if (Prefs(this).worker) loop = thread(name = "argus-worker", isDaemon = true) { work() }
            inboxLoop = thread(name = "argus-inbox", isDaemon = true) { inbox() }
            // a network came back (wifi, Tailscale, mobile data): don't wait out the back-off
            val cb = object : ConnectivityManager.NetworkCallback() {
                override fun onAvailable(network: Network) {
                    workerPause.kick()
                    inboxPause.kick()
                }
            }
            runCatching { getSystemService(ConnectivityManager::class.java).registerDefaultNetworkCallback(cb) }
            netCallback = cb
        }
        KeepAlive.schedule(this)
        return START_STICKY
    }

    override fun onDestroy() {
        running = false
        loop?.interrupt()
        inboxLoop?.interrupt()
        netCallback?.let { runCatching { getSystemService(ConnectivityManager::class.java).unregisterNetworkCallback(it) } }
        super.onDestroy()
    }

    /** The notification inbox: what Argus has for the phone, shown as notifications, then acknowledged. */
    private fun inbox() {
        val prefs = Prefs(this)
        val api = Api(prefs.url, prefs.token)
        val device = workerId()
        var backoff = 5_000L
        while (running) {
            try {
                val r = api.call("GET", "/phone/inbox?device=$device&wait=25&limit=20", timeoutMs = 45_000)
                if (r.status == 401 || r.status == 403) {
                    inboxPause.sleep(60_000)
                    continue
                }
                if (r.status >= 400) throw IOException("inbox: HTTP ${r.status}")
                backoff = 5_000L
                val msgs = r.json().optJSONArray("messages") ?: JSONArray()
                if (msgs.length() == 0) continue
                val shown = JSONArray()
                for (i in 0 until msgs.length()) {
                    val m = msgs.getJSONObject(i)
                    try {
                        Notifier.show(this, m)
                        shown.put(m.getString("id"))
                    } catch (e: Exception) {
                        Log.w(TAG, "can't show a notification", e)  // left queued: shown once Android allows it
                    }
                }
                if (shown.length() > 0) api.post("/phone/inbox/ack", JSONObject().put("ids", shown))
                if (shown.length() == 0) inboxPause.sleep(30_000)
            } catch (e: InterruptedException) {
                return
            } catch (e: Exception) {
                Log.w(TAG, "inbox: can't reach Argus", e)
                try {
                    inboxPause.sleep(backoff)
                } catch (_: InterruptedException) {
                    return
                }
                backoff = minOf(backoff * 2, 120_000L)
            }
        }
    }

    private fun show(text: String) {
        lastText = text
        val open = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE)
        val n = Notification.Builder(this, CHANNEL_ON)
            .setSmallIcon(android.R.drawable.presence_online)
            .setContentTitle("Argus")
            .setContentText(text)
            .setContentIntent(open)
            .setOngoing(true)
            .build()
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(1, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        } else {
            startForeground(1, n)
        }
    }

    private fun workerId(): String =
        "phone-" + (Build.MODEL ?: "android").lowercase().replace(Regex("[^a-z0-9]+"), "-").trim('-')

    private fun work() {
        val prefs = Prefs(this)
        val api = Api(prefs.url, prefs.token)
        val actions = Actions(this)
        val id = workerId()
        val caps = JSONArray().put("phone")
        var backoff = 5_000L
        var registeredAt = 0L
        while (running) {
            try {
                if (System.currentTimeMillis() - registeredAt > 10 * 60_000) {
                    val r = api.post("/workers/register", JSONObject().put("id", id).put("host", Build.MODEL ?: "phone")
                        .put("capabilities", caps).put("version", "app-1.1"))
                    if (r.status == 401 || r.status == 403) {
                        show("the token was refused: open Argus to fix it")
                        workerPause.sleep(60_000)
                        continue
                    }
                    if (r.status >= 400) throw IOException("register: HTTP ${r.status}")
                    registeredAt = System.currentTimeMillis()
                    show("connected")
                }
                val claim = api.post("/workers/$id/claim", JSONObject().put("capabilities", caps).put("plugins", JSONArray().put("phone")).put("wait", 25),
                    timeoutMs = 45_000)
                backoff = 5_000L
                if (claim.status == 204) continue
                if (claim.status >= 400) throw IOException("claim: HTTP ${claim.status}")
                runJob(api, actions, id, claim.json())
            } catch (e: InterruptedException) {
                return
            } catch (e: Exception) {
                Log.w(TAG, "can't reach Argus", e)
                show("can't reach Argus, trying again…")
                registeredAt = 0L
                try {
                    workerPause.sleep(backoff)
                } catch (_: InterruptedException) {
                    return
                }
                backoff = minOf(backoff * 2, 120_000L)
            }
        }
    }

    private fun runJob(api: Api, actions: Actions, worker: String, job: JSONObject) {
        val jobId = job.getString("id")
        val who = JSONObject().put("worker", worker)
        api.post("/jobs/$jobId/start", who)
        val live = job.optJSONObject("plugin_info")?.optBoolean("live", false) ?: false
        val workflow = job.optString("workflow")
        try {
            val result = if (job.optString("plugin") == "phone") {
                actions.run(workflow, job.optJSONObject("input") ?: JSONObject(), live)
            } else {
                throw ActionError("the phone only runs the phone plugin")
            }
            api.post("/jobs/$jobId/succeed", JSONObject().put("worker", worker).put("result", result))
            show("connected · last: $workflow")
        } catch (e: Exception) {
            val msg = if (e is ActionError) e.message ?: "failed" else "${e.javaClass.simpleName}: ${e.message}"
            api.post("/jobs/$jobId/fail", JSONObject().put("worker", worker).put("error", msg.take(400))
                .put("retryable", false))
        }
    }
}
