package dev.argus.companion.send

import android.content.Context
import android.util.Log
import java.io.File

enum class OutboxAction { KEEP, DELETE, REMOVE_AND_DELETE }

/** Pure rules for the copies of shared files in filesDir/outbox/<share-id>/. */
object OutboxRules {
    const val GRACE_MS = 60L * 60 * 1000                 // enqueue makes the folder before the queue row exists
    const val MAX_AGE_MS = 30L * 24 * 60 * 60 * 1000
    const val CAP_BYTES = 500L shl 20
    const val TARGET_BYTES = 400L shl 20
    const val EVERY_MS = 24L * 60 * 60 * 1000

    /** state null = no queue row. newest = newest file mtime in the folder. */
    fun decide(state: SendState?, newest: Long, now: Long): OutboxAction {
        if (newest > now || now - newest < GRACE_MS) return OutboxAction.KEEP   // fresh, or the clock moved: keep
        if (now - newest < MAX_AGE_MS) return OutboxAction.KEEP
        return when (state) {
            null, SendState.CANCELLED -> OutboxAction.DELETE
            SendState.FAILED -> OutboxAction.REMOVE_AND_DELETE
            else -> OutboxAction.KEEP
        }
    }

    /** May be deleted to save space: no row or CANCELLED, past the grace, not from the future. */
    fun spaceDeletable(state: SendState?, newest: Long, now: Long): Boolean =
        (state == null || state == SendState.CANCELLED) && newest <= now && now - newest >= GRACE_MS

    class Folder(val id: String, val newest: Long, val bytes: Long, val state: SendState?)

    /** Ids to delete (oldest first) when the total is over CAP_BYTES, until under TARGET_BYTES. */
    fun overCap(folders: List<Folder>, now: Long): List<String> {
        var total = folders.sumOf { it.bytes }
        if (total <= CAP_BYTES) return emptyList()
        val out = ArrayList<String>()
        for (f in folders.filter { spaceDeletable(it.state, it.newest, now) }.sortedBy { it.newest }) {
            if (total < TARGET_BYTES) break
            out += f.id; total -= f.bytes
        }
        return out
    }
}

object OutboxCleanup {
    private fun newest(dir: File): Long {
        var m = dir.lastModified()
        dir.listFiles()?.forEach { m = maxOf(m, it.lastModified()) }
        return m
    }

    private fun size(dir: File) = dir.listFiles()?.sumOf { it.length() } ?: 0L

    /** Lists folders first, then reads the rows, all while holding the queue lock so SendQueue.add cannot interleave. */
    fun cleanOutbox(ctx: Context, now: Long = System.currentTimeMillis()) {
        val root = File(ctx.filesDir, "outbox")
        val dirs = root.listFiles()?.filter { it.isDirectory } ?: return
        val queue = SendQueue.get(ctx)
        synchronized(queue) {  // SendQueue's @Synchronized methods (add, remove, shares) lock the instance
            val rows = queue.shares().associateBy { it.id }
            val folders = ArrayList<OutboxRules.Folder>()
            for (d in dirs) {
                val st = rows[d.name]?.state; val nw = newest(d)
                when (OutboxRules.decide(st, nw, now)) {
                    OutboxAction.KEEP -> folders += OutboxRules.Folder(d.name, nw, size(d), st)
                    OutboxAction.DELETE -> d.deleteRecursively()
                    OutboxAction.REMOVE_AND_DELETE -> { queue.remove(d.name); d.deleteRecursively() }
                }
            }
            for (id in OutboxRules.overCap(folders, now)) File(root, id).deleteRecursively()
        }
    }

    /** Runs cleanOutbox on the first call (lastClean unset) and then at most once a day. Never throws. */
    fun maybeClean(ctx: Context, force: Boolean = false) {
        try {
            val p = ctx.getSharedPreferences("argus", Context.MODE_PRIVATE)
            val now = System.currentTimeMillis(); val last = p.getLong("outbox_last_clean", 0)
            if (!force && last in 1..now && now - last < OutboxRules.EVERY_MS) return
            cleanOutbox(ctx, now)
            p.edit().putLong("outbox_last_clean", now).apply()
        } catch (e: Exception) { Log.w("argus-send", "outbox cleanup", e) }
    }
}
