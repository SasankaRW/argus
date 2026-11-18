package dev.argus.companion.send

import android.content.ActivityNotFoundException
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.provider.MediaStore
import android.webkit.MimeTypeMap
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/**
 * What this phone sent and received, for Helios's Transfers page in the app, and opening those files here. Read
 * straight from the phone's own records (SendQueue.mine), so it works while Argus is down.
 *  - received: saved in Download/Argus/From <device>/ (the path the receiver recorded, found again in MediaStore)
 *  - sent: the app's own copy (files up to 64 MB are copied when shared) or the other app's link, while it lasts
 */
object PhoneMine {
    private fun provided(ctx: Context, f: File): Uri? =
        if (f.isFile) runCatching { SendFiles.uriFor(ctx, f) }.getOrNull() else null

    private fun resolve(ctx: Context, kind: String, stored: String): Uri? {
        if (stored.isBlank()) return null
        if (stored.startsWith("content://")) return Uri.parse(stored)
        if (stored.startsWith("file://")) return provided(ctx, File(Uri.parse(stored).path ?: return null))
        if (kind == "received" && Build.VERSION.SDK_INT >= 29 && !stored.startsWith("/")) {
            // "Download/Argus/From PC/photos/a.jpg": the MediaStore row this app inserted
            val dir = stored.substringBeforeLast('/') + "/"
            val name = stored.substringAfterLast('/')
            ctx.contentResolver.query(MediaStore.Downloads.EXTERNAL_CONTENT_URI, arrayOf(MediaStore.MediaColumns._ID),
                "${MediaStore.MediaColumns.RELATIVE_PATH}=? AND ${MediaStore.MediaColumns.DISPLAY_NAME}=?", arrayOf(dir, name), null)?.use {
                if (it.moveToFirst()) return Uri.withAppendedPath(MediaStore.Downloads.EXTERNAL_CONTENT_URI, it.getLong(0).toString())
            }
            return null
        }
        val path = if (stored.startsWith("/")) stored
            else File(Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS), stored.removePrefix("Download/")).path
        return provided(ctx, File(path))
    }

    private fun reachable(ctx: Context, u: Uri?): Boolean =
        u != null && runCatching { ctx.contentResolver.openFileDescriptor(u, "r")?.use { true } ?: false }.getOrDefault(false)

    fun json(ctx: Context): String {
        val items = JSONArray()
        for (r in SendQueue.get(ctx).mine()) {
            val files = JSONArray()
            for (f in r.files) files.put(JSONObject().put("n", f.n).put("name", f.name).put("size", f.size)
                .put("here", reachable(ctx, resolve(ctx, r.kind, f.uri))))
            items.put(JSONObject().put("kind", r.kind).put("offer_id", r.offerId).put("at", r.at / 1000.0)
                .put("label", r.label).put("peer", r.peer).put("files", files))
        }
        return JSONObject().put("device", SendHub.SELF).put("items", items).toString()
    }

    /** Opens one file in the app that handles it. "" when it opened, else why not (shown under the file). */
    fun open(ctx: Context, kind: String, offerId: String, n: Int): String {
        val f = SendQueue.get(ctx).mineFiles(kind, offerId).firstOrNull { it.n == n } ?: return "This phone has no record of that file"
        val u = resolve(ctx, kind, f.uri)
        if (u == null || !reachable(ctx, u)) {
            return if (kind == "sent") "The original isn't reachable any more (moved, deleted, or the app that shared it took it back)"
            else "That file was moved or deleted"
        }
        val mime = ctx.contentResolver.getType(u)
            ?: MimeTypeMap.getSingleton().getMimeTypeFromExtension(f.name.substringAfterLast('.', "").lowercase()) ?: "*/*"
        val view = Intent(Intent.ACTION_VIEW).setDataAndType(u, mime).addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        return try {
            ctx.startActivity(Intent.createChooser(view, f.name.substringAfterLast('/')).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)); ""
        } catch (_: ActivityNotFoundException) { "No app on this phone opens this kind of file" }
    }
}
