package dev.argus.companion.send

import android.app.Activity
import android.app.PendingIntent
import android.content.ClipboardManager
import android.content.Intent
import android.os.Build
import android.os.Bundle
import android.service.quicksettings.Tile
import android.service.quicksettings.TileService
import kotlin.concurrent.thread

/** Quick-settings tile "Send clipboard". Inactive when sync is off. */
class ClipTile : TileService() {
    override fun onStartListening() { refresh() }

    private fun refresh() {
        val t = qsTile ?: return
        t.state = if (ClipHub.enabled(this)) Tile.STATE_ACTIVE else Tile.STATE_INACTIVE
        t.updateTile()
    }

    @Suppress("DEPRECATION")
    override fun onClick() {
        if (!ClipHub.enabled(this)) { ClipHub.toast(this, "Clipboard sync is off"); return }
        val i = Intent(this, ClipActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        if (Build.VERSION.SDK_INT >= 34)
            startActivityAndCollapse(PendingIntent.getActivity(this, 0, i, PendingIntent.FLAG_IMMUTABLE))
        else startActivityAndCollapse(i)
    }
}

/**
 * Transparent, no-animation: Android lets an app read the clipboard only while it has focus, so this reads it in
 * onWindowFocusChanged(true), hands it to the sender, and finishes.
 */
class ClipActivity : Activity() {
    private var done = false

    override fun onCreate(b: Bundle?) { super.onCreate(b); overridePendingTransition(0, 0) }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        if (!hasFocus || done) return
        done = true
        val app = applicationContext
        val text = runCatching {
            getSystemService(ClipboardManager::class.java).primaryClip?.takeIf { it.itemCount > 0 }?.getItemAt(0)?.coerceToText(this)?.toString()
        }.getOrNull()
        when {
            !ClipHub.enabled(app) -> ClipHub.toast(app, "Clipboard sync is off")
            text.isNullOrEmpty() -> ClipHub.toast(app, "Clipboard is empty")
            !ClipRules.sizeOk(text) -> ClipHub.toast(app, "Clipboard is too big to send")
            else -> thread { ClipHub.toast(app, if (SendHub.sendClip(app, "copy", text)) "Sent to PC" else "Couldn't reach the PC") }
        }
        finish()
        overridePendingTransition(0, 0)
    }
}
