package dev.argus.companion

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** Connects to Argus again after the phone restarts. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == Intent.ACTION_BOOT_COMPLETED) WorkerService.start(context)
    }
}
