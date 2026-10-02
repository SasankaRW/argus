package dev.argus.companion

import android.app.AlarmManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/**
 * Every 15 minutes: if Android stopped the connection service (memory, a battery saver), start it again, so
 * notifications keep coming with the app closed. Starting it from the background is allowed once Argus is
 * exempt from battery optimisation (the setup page asks for that).
 */
class KeepAlive : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        runCatching { WorkerService.start(context) }
        schedule(context)
    }

    companion object {
        fun schedule(ctx: Context) {
            if (!Prefs(ctx).ready) return
            val am = ctx.getSystemService(AlarmManager::class.java)
            val pi = PendingIntent.getBroadcast(ctx, 7, Intent(ctx, KeepAlive::class.java),
                PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
            am.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, System.currentTimeMillis() + 15 * 60_000L, pi)
        }
    }
}
