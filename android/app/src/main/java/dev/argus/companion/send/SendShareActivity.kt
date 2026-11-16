package dev.argus.companion.send

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.widget.Toast
import dev.argus.companion.WorkerService
import kotlin.concurrent.thread

/** ACTION_SEND / SEND_MULTIPLE: queues the share and finishes at once (no UI). The direct-share targets arrive here too. */
class SendShareActivity : Activity() {
    override fun onCreate(b: Bundle?) {
        super.onCreate(b)
        val i = intent
        val target = i.getStringExtra(SendHub.EXTRA_TARGET) ?: i.getStringExtra(Intent.EXTRA_SHORTCUT_ID)?.removePrefix("send-")
            ?: getSharedPreferences("argus", Context.MODE_PRIVATE).getString("send_last", "calypso") ?: "calypso"
        val uris = ArrayList<Uri>()
        @Suppress("DEPRECATION")
        if (i.action == Intent.ACTION_SEND_MULTIPLE) i.getParcelableArrayListExtra<Uri>(Intent.EXTRA_STREAM)?.let { uris += it }
        else (i.getParcelableExtra<Uri>(Intent.EXTRA_STREAM))?.let { uris += it }
        i.clipData?.let { c -> for (k in 0 until c.itemCount) c.getItemAt(k).uri?.let { if (it !in uris) uris += it } }
        val ctx = applicationContext
        // the copy of small files must finish before the other app's permission lapses: do it now, then close
        val n = SendHub.enqueue(ctx, target, uris)
        if (n > 0) { WorkerService.startSend(ctx); Toast.makeText(ctx, "Queued for ${target.replaceFirstChar { it.uppercase() }.let { if (it == "Pc") "PC" else it }}", Toast.LENGTH_SHORT).show() }
        finish()
    }
}
