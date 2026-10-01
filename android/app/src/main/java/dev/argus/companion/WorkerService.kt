package dev.argus.companion

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.util.Log
import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import kotlin.concurrent.thread

/**
 * Keeps the phone connected to Argus as a worker with the capability "phone": asks for a phone job (waiting up to
 * 25 s), runs it, reports the result, and asks again. Only outgoing requests to your Argus; nothing listens.
 */
class WorkerService : Service() {

    companion object {
        const val CHANNEL_ON = "argus-on"
        const val CHANNEL_ASK = "argus-ask"
        private const val TAG = "argus-worker"

        fun start(ctx: Context) {
            if (!Prefs(ctx).ready || !Prefs(ctx).worker) return
            ctx.startForegroundService(Intent(ctx, WorkerService::class.java))
        }

        fun stop(ctx: Context) = ctx.stopService(Intent(ctx, WorkerService::class.java))

        fun channels(ctx: Context) {
            val nm = ctx.getSystemService(NotificationManager::class.java)
            nm.createNotificationChannel(NotificationChannel(CHANNEL_ON, "Connected to Argus",
                NotificationManager.IMPORTANCE_MIN).apply { setShowBadge(false) })
            nm.createNotificationChannel(NotificationChannel(CHANNEL_ASK, "Ari asks",
                NotificationManager.IMPORTANCE_HIGH))
        }
    }

    @Volatile private var running = false
    private var loop: Thread? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        channels(this)
        show("connecting…")
        if (!running) {
            running = true
            loop = thread(name = "argus-worker", isDaemon = true) { work() }
        }
        return START_STICKY
    }

    override fun onDestroy() {
        running = false
        loop?.interrupt()
        super.onDestroy()
    }

    private fun show(text: String) {
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
                        .put("capabilities", caps).put("version", "app-1.0"))
                    if (r.status == 401 || r.status == 403) {
                        show("the token was refused: open Argus to fix it")
                        Thread.sleep(60_000)
                        continue
                    }
                    if (r.status >= 400) throw IOException("register: HTTP ${r.status}")
                    registeredAt = System.currentTimeMillis()
                    show("connected")
                }
                val claim = api.post("/workers/$id/claim", JSONObject().put("capabilities", caps).put("wait", 25),
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
                    Thread.sleep(backoff)
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
