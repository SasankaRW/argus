package dev.argus.companion

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** Connects to Argus again after the phone restarts or the app is updated. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == Intent.ACTION_BOOT_COMPLETED || intent.action == Intent.ACTION_MY_PACKAGE_REPLACED) {
            runCatching { WorkerService.start(context) }
            KeepAlive.schedule(context)
        }
    }
}
