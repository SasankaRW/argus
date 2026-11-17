package dev.argus.companion.send

import android.app.DownloadManager
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** Send's notifications: one quiet progress channel (speed + Cancel) and one for results. */
object SendNotes {
    const val CH_PROGRESS = "argus-send-progress"
    const val CH_RESULT = "argus-send"

    fun channels(ctx: Context) {
        val nm = ctx.getSystemService(NotificationManager::class.java)
        if (nm.getNotificationChannel(CH_PROGRESS) == null)
            nm.createNotificationChannel(NotificationChannel(CH_PROGRESS, "File transfers", NotificationManager.IMPORTANCE_LOW).apply { setShowBadge(false) })
        if (nm.getNotificationChannel(CH_RESULT) == null)
            nm.createNotificationChannel(NotificationChannel(CH_RESULT, "Sent and received files", NotificationManager.IMPORTANCE_DEFAULT))
    }

    private fun id(share: String) = 2000 + (share.hashCode() and 0x7fffff)

    private fun action(ctx: Context, what: String, share: String, label: String): Notification.Action {
        val i = Intent(ctx, SendActionReceiver::class.java).setAction("dev.argus.companion.SEND_$what").putExtra("share", share)
        val pi = PendingIntent.getBroadcast(ctx, id(share) + what.hashCode() % 1000, i, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        return Notification.Action.Builder(null, label, pi).build()
    }

    private fun post(ctx: Context, nid: Int, b: Notification.Builder) {
        val nm = ctx.getSystemService(NotificationManager::class.java)
        if (nm.areNotificationsEnabled()) nm.notify(nid, b.build())
    }

    private fun base(ctx: Context, ch: String, title: String, text: String) = Notification.Builder(ctx, ch)
        .setSmallIcon(android.R.drawable.stat_sys_upload_done).setContentTitle(title).setContentText(text)

    /** The receiving device's icon: it rides the bar on Android 16+ (ProgressStyle's tracker). */
    private fun tracker(peerId: String) = when {
        peerId == "pc" || "desktop" in peerId -> dev.argus.companion.R.drawable.ic_send_to_pc
        peerId == "calypso" || "laptop" in peerId -> dev.argus.companion.R.drawable.ic_send_to_laptop
        else -> dev.argus.companion.R.drawable.ic_send_to_device
    }

    fun progress(ctx: Context, share: String, peer: String, done: Long, total: Long, bytesPerSec: Long, peerId: String = "") {
        val pct = if (total > 0) (done * 100 / total).toInt().coerceIn(0, 100) else 0
        val speed = if (bytesPerSec > 0) " · ${human(bytesPerSec)}/s" else ""
        val left = if (bytesPerSec > 0 && total > done) ((total - done) / bytesPerSec).let { s -> if (s < 60) " · ${maxOf(1L, s)} s left" else " · ${s / 60} min left" } else ""
        val b = base(ctx, CH_PROGRESS, "Sending to $peer", "$pct% · ${human(done)} of ${human(total)}$speed$left")
            .setSmallIcon(android.R.drawable.stat_sys_upload).setOngoing(true).setOnlyAlertOnce(true)
            .addAction(action(ctx, "CANCEL", share, "Cancel"))
        if (android.os.Build.VERSION.SDK_INT >= 36) {
            // Android 16+: the progress-centric notification, the device riding the bar, shown as a live update
            b.setStyle(Notification.ProgressStyle().setProgress(pct).setStyledByProgress(true)
                .setProgressTrackerIcon(android.graphics.drawable.Icon.createWithResource(ctx, tracker(peerId))))
            b.addExtras(android.os.Bundle().apply { putBoolean("android.requestPromotedOngoing", true) })  // a live update
        } else b.setProgress(100, pct, false)
        post(ctx, id(share), b)
    }

    fun sent(ctx: Context, share: String, peer: String) { post(ctx, id(share), base(ctx, CH_RESULT, "Sent to $peer", "Delivered and checked").setAutoCancel(true)) }

    fun waiting(ctx: Context, share: String, peer: String, why: String = "It will go as soon as it is back.", wifi: Boolean = false) {
        val b = base(ctx, CH_PROGRESS, "Waiting for $peer", why).setSmallIcon(android.R.drawable.stat_sys_upload).setOngoing(false)
            .addAction(action(ctx, "CANCEL", share, "Cancel"))
        if (wifi) b.addAction(action(ctx, "ANYWAY", share, "Send anyway"))
        post(ctx, id(share), b)
    }

    fun keepWaiting(ctx: Context, share: String, peer: String) {
        post(ctx, id(share), base(ctx, CH_RESULT, "Still waiting for $peer", "Waiting for a week. Keep waiting?")
            .addAction(action(ctx, "KEEP", share, "Keep waiting")).addAction(action(ctx, "CANCEL", share, "Cancel")))
    }

    fun failed(ctx: Context, share: String, peer: String, reason: String) {
        post(ctx, id(share), base(ctx, CH_RESULT, "Could not send to $peer", reason).addAction(action(ctx, "RETRY", share, "Try again")).setAutoCancel(true))
    }

    fun clear(ctx: Context, share: String) { ctx.getSystemService(NotificationManager::class.java).cancel(id(share)) }

    /** "3 files from PC" with Open (the Downloads app, where Download/Argus lives). */
    fun received(ctx: Context, count: Int, peerName: String, key: String) {
        val open = PendingIntent.getActivity(ctx, 0, Intent(DownloadManager.ACTION_VIEW_DOWNLOADS).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK), PendingIntent.FLAG_IMMUTABLE)
        val title = if (count == 1) "1 file from $peerName" else "$count files from $peerName"
        post(ctx, 3000 + (key.hashCode() and 0xffff), base(ctx, CH_RESULT, title, "Saved in Downloads/Argus")
            .setSmallIcon(android.R.drawable.stat_sys_download_done).setAutoCancel(true).setContentIntent(open)
            .addAction(Notification.Action.Builder(null, "Open", open).build()))
    }

    fun human(b: Long): String = when {
        b >= 1L shl 30 -> "%.1f GB".format(b / 1073741824.0)
        b >= 1L shl 20 -> "%.1f MB".format(b / 1048576.0)
        b >= 1L shl 10 -> "%d KB".format(b shr 10)
        else -> "$b B"
    }
}

/** Buttons on Send notifications: Cancel, Try again, Send anyway, Keep waiting. */
class SendActionReceiver : BroadcastReceiver() {
    override fun onReceive(ctx: Context, intent: Intent) {
        val id = intent.getStringExtra("share") ?: return
        val q = SendQueue.get(ctx)
        when (intent.action?.removePrefix("dev.argus.companion.SEND_")) {
            "CANCEL" -> { SendHub.cancel(id); q.move(id, SendState.CANCELLED); SendNotes.clear(ctx, id) }
            "RETRY" -> { q.move(id, SendState.QUEUED); SendNotes.clear(ctx, id) }
            "ANYWAY" -> { q.sendAnyway(id); SendNotes.clear(ctx, id) }
            "KEEP" -> { q.keepWaiting(id); SendNotes.clear(ctx, id) }
        }
        SendHub.kick()
    }
}
