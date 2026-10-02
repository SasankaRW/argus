package dev.argus.companion

import android.app.Notification
import android.app.NotificationManager
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import kotlin.concurrent.thread

/**
 * A notification's button (Approve, Reject): calls Argus with the one-time link that came with it, then clears the
 * notification, or says why it didn't work.
 */
class ActionReceiver : BroadcastReceiver() {
    override fun onReceive(ctx: Context, intent: Intent) {
        val url = intent.getStringExtra("url") ?: return
        val method = intent.getStringExtra("method") ?: "POST"
        val nid = intent.getIntExtra("nid", 0)
        val label = intent.getStringExtra("label") ?: "Argus"
        val clear = intent.getBooleanExtra("clear", true)
        val pending = goAsync()
        thread(name = "argus-action") {
            val nm = ctx.getSystemService(NotificationManager::class.java)
            try {
                val prefs = Prefs(ctx)
                val full = if (url.startsWith("/")) prefs.url + url else url
                val mine = prefs.url.isNotEmpty() && full.startsWith(prefs.url)
                val api = if (mine) Api(prefs.url, prefs.token) else Api("", "")  // the token only goes to your Argus
                val r = api.call(method, if (mine) full.removePrefix(prefs.url) else full,
                    if (method == "GET") null else org.json.JSONObject())
                when {
                    r.status in 200..299 -> if (clear) nm.cancel(nid)
                    r.status == 409 -> report(ctx, nm, nid, "$label: already decided")
                    r.status in 401..403 -> report(ctx, nm, nid, "$label: this link no longer works")
                    else -> report(ctx, nm, nid, "$label failed (HTTP ${r.status})")
                }
            } catch (e: Exception) {
                report(ctx, nm, nid, "$label failed: can't reach Argus (is Tailscale on?)")
            } finally {
                pending.finish()
            }
        }
    }

    private fun report(ctx: Context, nm: NotificationManager, nid: Int, text: String) {
        nm.cancel(nid)
        nm.notify(nid, Notification.Builder(ctx, "argus-default").setSmallIcon(android.R.drawable.ic_dialog_info)
            .setContentTitle("Argus").setContentText(text).setAutoCancel(true).build())
    }
}
