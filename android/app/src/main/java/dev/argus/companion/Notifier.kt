package dev.argus.companion

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.media.AudioAttributes
import android.media.RingtoneManager
import org.json.JSONObject

/**
 * Shows what Argus sends as notifications (approvals, failures, reminders, summaries, "ring my phone"). The app
 * collects them itself (Inbox in WorkerService), so they arrive with the app closed; nothing goes through any
 * outside service.
 */
object Notifier {

    private fun channel(priority: Int) = when {
        priority >= 5 -> "argus-urgent"
        priority == 4 -> "argus-high"
        priority == 3 -> "argus-default"
        priority == 2 -> "argus-low"
        else -> "argus-min"
    }

    fun channels(ctx: Context) {
        val nm = ctx.getSystemService(NotificationManager::class.java)
        fun make(id: String, name: String, importance: Int, block: NotificationChannel.() -> Unit = {}) {
            if (nm.getNotificationChannel(id) == null) {
                nm.createNotificationChannel(NotificationChannel(id, name, importance).apply(block))
            }
        }
        make("argus-min", "Quiet", NotificationManager.IMPORTANCE_MIN) { setShowBadge(false) }
        make("argus-low", "Low", NotificationManager.IMPORTANCE_LOW)
        make("argus-default", "Messages", NotificationManager.IMPORTANCE_DEFAULT)
        make("argus-high", "Approvals and warnings", NotificationManager.IMPORTANCE_HIGH)
        make("argus-urgent", "Urgent (alarm sound)", NotificationManager.IMPORTANCE_HIGH) {
            setSound(
                RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM),
                AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_ALARM)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build()
            )
            enableVibration(true)
            if (nm.isNotificationPolicyAccessGranted) setBypassDnd(true)
        }
    }

    /** Throws when Android won't show it (notifications switched off): the caller then leaves the message queued. */
    fun show(ctx: Context, msg: JSONObject) {
        val nm = ctx.getSystemService(NotificationManager::class.java)
        if (!nm.areNotificationsEnabled()) throw IllegalStateException("notifications are off for Argus")
        channels(ctx)
        val priority = msg.optInt("priority", 3)
        val id = 100 + (msg.optString("id").hashCode() and 0x7fffffff) % 1_000_000
        val title = msg.optString("title", "Argus")
        val text = msg.optString("message", "")
        val b = Notification.Builder(ctx, channel(priority))
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setContentTitle(title)
            .setContentText(text)
            .setStyle(Notification.BigTextStyle().bigText(text))
            .setAutoCancel(true)
            .setShowWhen(true)
            .setWhen((msg.optDouble("at", System.currentTimeMillis() / 1000.0) * 1000).toLong())
            .setContentIntent(open(ctx, id, msg.optString("click")))
        if (priority >= 5) b.setCategory(Notification.CATEGORY_ALARM)
        val actions = msg.optJSONArray("actions")
        for (i in 0 until minOf(actions?.length() ?: 0, 3)) {
            val a = actions!!.getJSONObject(i)
            val label = a.optString("label", "Open")
            val url = a.optString("url", "")
            val pi = if (a.optString("action") == "view") {
                open(ctx, id * 10 + i, url)
            } else {
                PendingIntent.getBroadcast(
                    ctx, id * 10 + i,
                    Intent(ctx, ActionReceiver::class.java).putExtra("nid", id).putExtra("url", url)
                        .putExtra("method", a.optString("method", "POST")).putExtra("clear", a.optBoolean("clear", true))
                        .putExtra("label", label),
                    PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
                )
            }
            @Suppress("DEPRECATION")
            b.addAction(Notification.Action.Builder(0, label, pi).build())
        }
        nm.notify(id, b.build())
        // "Ring my phone": urgent plus the rotating-light tag also plays the loud alarm (volume to the top)
        val tags = msg.optJSONArray("tags")
        val ring = (0 until (tags?.length() ?: 0)).any { tags!!.optString(it) == "rotating_light" }
        if (priority >= 5 && ring) runCatching { Actions(ctx).ring() }
    }

    /** Opens Argus (Helios) at a page: a path on Argus, or a full link. */
    private fun open(ctx: Context, code: Int, path: String): PendingIntent {
        val i = Intent(ctx, MainActivity::class.java).setAction("dev.argus.companion.OPEN").putExtra("path", path)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP)
        return PendingIntent.getActivity(ctx, code, i, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
    }
}
