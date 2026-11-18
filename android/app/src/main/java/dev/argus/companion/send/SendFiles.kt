package dev.argus.companion.send

import android.content.ContentProvider
import android.content.ContentValues
import android.content.Context
import android.database.Cursor
import android.database.MatrixCursor
import android.net.Uri
import android.os.Environment
import android.os.ParcelFileDescriptor
import android.provider.OpenableColumns
import android.webkit.MimeTypeMap
import java.io.File
import java.io.FileNotFoundException

/**
 * Hands a file Argus Send kept to another app to open (Helios's Transfers page, "Open"), read-only, and only from two
 * places: the copies of shared files (files/outbox/) and, on old Android, received files (Download/Argus/).
 * content://<app>.files/outbox/<share>/<n>  and  content://<app>.files/dl/Argus/From PC/a.jpg
 */
class SendFiles : ContentProvider() {
    companion object {
        fun uriFor(ctx: Context, f: File): Uri? {
            val c = f.canonicalFile
            val outbox = File(ctx.filesDir, "outbox").canonicalFile
            val dl = File(Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS), "Argus").canonicalFile
            val (root, tag) = when {
                c.path.startsWith(outbox.path + File.separator) -> outbox to "outbox"
                c.path.startsWith(dl.path + File.separator) -> dl.parentFile!! to "dl"
                else -> return null
            }
            val b = Uri.Builder().scheme("content").authority(ctx.packageName + ".files").appendPath(tag)
            c.relativeTo(root).invariantSeparatorsPath.split('/').forEach { b.appendPath(it) }
            return b.build()
        }
    }

    private fun fileOf(uri: Uri): File {
        val ctx = context ?: throw FileNotFoundException()
        val parts = uri.pathSegments
        if (parts.size < 2) throw FileNotFoundException()
        val root = when (parts[0]) {
            "outbox" -> File(ctx.filesDir, "outbox")
            "dl" -> Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS)
            else -> throw FileNotFoundException()
        }.canonicalFile
        val f = File(root, parts.drop(1).joinToString(File.separator)).canonicalFile
        val allowed = if (parts[0] == "dl") File(root, "Argus").path + File.separator else root.path + File.separator
        if (!f.path.startsWith(allowed) || !f.isFile) throw FileNotFoundException()
        return f
    }

    override fun onCreate() = true

    override fun openFile(uri: Uri, mode: String): ParcelFileDescriptor {
        if (mode != "r") throw SecurityException("read only")
        return ParcelFileDescriptor.open(fileOf(uri), ParcelFileDescriptor.MODE_READ_ONLY)
    }

    override fun getType(uri: Uri): String =
        MimeTypeMap.getSingleton().getMimeTypeFromExtension(uri.lastPathSegment?.substringAfterLast('.', "")?.lowercase() ?: "")
            ?: "application/octet-stream"

    override fun query(uri: Uri, projection: Array<out String>?, selection: String?, args: Array<out String>?, sort: String?): Cursor {
        val f = fileOf(uri)
        val cols = projection ?: arrayOf(OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE)
        return MatrixCursor(cols, 1).apply {
            addRow(cols.map { if (it == OpenableColumns.SIZE) f.length() else if (it == OpenableColumns.DISPLAY_NAME) f.name else null })
        }
    }

    override fun insert(uri: Uri, values: ContentValues?): Uri? = null
    override fun delete(uri: Uri, selection: String?, args: Array<out String>?) = 0
    override fun update(uri: Uri, values: ContentValues?, selection: String?, args: Array<out String>?) = 0
}
