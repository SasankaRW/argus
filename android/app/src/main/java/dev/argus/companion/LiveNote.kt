package dev.argus.companion

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context

/** The one ongoing "Live" notification: the top live activity from Argus. */
object LiveNote {
    const val CHANNEL = "argus-live"
    private const val NID = 4001

    fun channel(ctx: Context) {
        val nm = ctx.getSystemService(NotificationManager::class.java)
        if (nm.getNotificationChannel(CHANNEL) == null)
            nm.createNotificationChannel(NotificationChannel(CHANNEL, "Live activities", NotificationManager.IMPORTANCE_LOW).apply { setShowBadge(false) })
    }

    fun clear(ctx: Context) = ctx.getSystemService(NotificationManager::class.java).cancel(NID)

    fun show(ctx: Context, a: Activity) {
        val nm = ctx.getSystemService(NotificationManager::class.java)
        if (!nm.areNotificationsEnabled()) return
        channel(ctx)
        val b = Notification.Builder(ctx, CHANNEL)
            .setSmallIcon(android.R.drawable.ic_popup_sync)
            .setContentTitle(a.title)
            .setContentText(a.detail)
            .setOngoing(true).setOnlyAlertOnce(true)
            .setCategory(Notification.CATEGORY_PROGRESS)
            .setContentIntent(Notifier.open(ctx, NID, LiveRank.route(a)))
        val ends = a.endsAt
        if (a.kind == "timer" && ends != null) {
            // the system ticks the countdown: no wake-ups from the app
            b.setShowWhen(true).setWhen(ends * 1000).setUsesChronometer(true).setChronometerCountDown(true)
        } else b.setShowWhen(false)
        val pct = a.progress?.let { (it * 100).toInt().coerceIn(0, 100) }
        val bar = pct != null || a.kind != "timer"  // a timer without progress shows only the countdown
        if (android.os.Build.VERSION.SDK_INT >= 36) {
            if (bar) {
                val s = Notification.ProgressStyle().setStyledByProgress(true)
                if (pct == null) s.setProgressIndeterminate(true) else s.setProgress(pct)
                b.setStyle(s)
            }
            b.addExtras(android.os.Bundle().apply { putBoolean("android.requestPromotedOngoing", true) })
        } else if (bar) {
            if (pct == null) b.setProgress(0, 0, true) else b.setProgress(100, pct, false)
        }
        nm.notify(NID, b.build())
    }
}
