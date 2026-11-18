package dev.argus.companion.send

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.ClipData
import android.content.ClipDescription
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.PersistableBundle
import android.widget.Toast
import dev.argus.companion.Prefs
import org.json.JSONObject

/** Clipboard and link sync on the phone: the receiving side, the Links notifications and the helpers the senders share. Clip text is never logged or stored. */
object ClipHub {
    const val CH_LINKS = "argus-links"
    const val ACTION_COPY = "dev.argus.companion.CLIP_COPY"
    const val EXTRA_LINK = "link"

    val ledger = ClipLedger()
    private val main = Handler(Looper.getMainLooper())

    fun enabled(ctx: Context) = Prefs(ctx).clipSync

    fun toast(ctx: Context, text: String) {
        val app = ctx.applicationContext
        main.post { Toast.makeText(app, text, Toast.LENGTH_SHORT).show() }
    }

    fun channel(ctx: Context) {
        val nm = ctx.getSystemService(NotificationManager::class.java)
        if (nm.getNotificationChannel(CH_LINKS) == null)
            nm.createNotificationChannel(NotificationChannel(CH_LINKS, "Links", NotificationManager.IMPORTANCE_DEFAULT))
    }

    /** Puts text on the clipboard, flagged sensitive on Android 13+ so the system preview hides it; the echo guard remembers it. */
    fun writeClipboard(ctx: Context, text: String) {
        ledger.wrote(text)
        val clip = ClipData.newPlainText("Argus", text)
        if (Build.VERSION.SDK_INT >= 33)
            clip.description.extras = PersistableBundle().apply { putBoolean(ClipDescription.EXTRA_IS_SENSITIVE, true) }
        val app = ctx.applicationContext
        main.post { runCatching { app.getSystemService(ClipboardManager::class.java).setPrimaryClip(clip) } }
    }

    /** POST /send/clip, after the session MAC has passed. Returns the HTTP status and body. */
    fun receive(ctx: Context, peer: String, sessionKey: ByteArray, body: ByteArray): Pair<Int, JSONObject> {
        if (!enabled(ctx)) return 409 to JSONObject().put("detail", "clipboard sync is off")
        val j = try { JSONObject(String(ClipCrypto.open(sessionKey, String(body)))) }
        catch (e: Exception) { return 400 to JSONObject().put("detail", "bad clip") }  // no message from e: it could carry text
        val id = j.optString("id"); val kind = j.optString("kind"); val text = j.optString("text")
        val from = j.optString("from").ifBlank { peer }
        if (id.isEmpty() || (kind != "copy" && kind != "link") || !ClipRules.sizeOk(text)) return 400 to JSONObject().put("detail", "bad clip")
        // the origin may differ from the session peer only when the peer is the holder (Calypso, relaying)
        if (from != peer && peer != "calypso") return 403 to JSONObject().put("detail", "clip from someone else")
        if (!ledger.accept(id, from, j.optLong("at", System.currentTimeMillis()))) return 200 to JSONObject().put("ok", true).put("ignored", true)
        if (kind == "link" && ClipRules.isLink(text)) postLink(ctx, text.trim()) else writeClipboard(ctx, text)
        return 200 to JSONObject().put("ok", true)
    }

    private fun nid(link: String) = 4000 + (link.hashCode() and 0xffff)

    private fun postLink(ctx: Context, link: String) {
        channel(ctx)
        val nm = ctx.getSystemService(NotificationManager::class.java)
        if (!nm.areNotificationsEnabled()) return
        val id = nid(link)
        val view = Intent(Intent.ACTION_VIEW, Uri.parse(link)).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        val open = PendingIntent.getActivity(ctx, id, view, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val copy = PendingIntent.getBroadcast(ctx, id,
            Intent(ctx, ClipCopyReceiver::class.java).setAction(ACTION_COPY).putExtra(EXTRA_LINK, link).putExtra("nid", id),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        nm.notify(id, Notification.Builder(ctx, CH_LINKS)
            .setSmallIcon(android.R.drawable.stat_sys_download_done).setContentTitle("Link from PC").setContentText(ClipRules.shortLink(link))
            .setAutoCancel(true).setContentIntent(open)
            .addAction(Notification.Action.Builder(null, "Open", open).build())
            .addAction(Notification.Action.Builder(null, "Copy", copy).build()).build())
    }
}

/** The Copy button on a link notification. */
class ClipCopyReceiver : BroadcastReceiver() {
    override fun onReceive(ctx: Context, intent: Intent) {
        val link = intent.getStringExtra(ClipHub.EXTRA_LINK) ?: return
        if (!ClipRules.isLink(link)) return
        ClipHub.writeClipboard(ctx, link)
        ctx.getSystemService(NotificationManager::class.java).cancel(intent.getIntExtra("nid", 0))
        ClipHub.toast(ctx, "Link copied")
    }
}
