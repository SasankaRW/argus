package dev.argus.companion

import org.json.JSONArray
import org.json.JSONObject

/** One live activity as Argus reports it (GET /activities). */
data class Activity(
    val id: String,
    val kind: String,
    val title: String,
    val detail: String,
    val icon: String,
    val progress: Double?,
    val endsAt: Long?,
    val startedAt: Double,
    val device: String?,
    val devices: List<String> = emptyList(),  // a transfer's two ends
) {
    companion object {
        fun parse(o: JSONObject) = Activity(
            id = o.optString("id"),
            kind = o.optString("kind", "custom"),
            title = o.optString("title"),
            detail = o.optString("detail"),
            icon = o.optString("icon"),
            progress = if (o.isNull("progress")) null else o.optDouble("progress").coerceIn(0.0, 1.0),
            endsAt = if (o.isNull("ends_at")) null else o.optDouble("ends_at").toLong(),
            startedAt = o.optDouble("started_at", 0.0),
            device = if (o.isNull("device")) null else o.optString("device").ifBlank { null },
            devices = o.optJSONArray("devices")?.let { arr -> List(arr.length()) { arr.optString(it) } } ?: emptyList(),
        )

        fun parseAll(a: JSONArray?): List<Activity> =
            (0 until (a?.length() ?: 0)).mapNotNull { runCatching { parse(a!!.getJSONObject(it)) }.getOrNull() }
    }
}

/**
 * Which activity the phone's one "Live" notification shows. Same order as the PC island: a timer under 60 s from
 * its end, transfers, timers, jobs, custom; ties go to the newest. Skips what the phone already shows itself:
 * transfers this phone is part of (SendNotes) and phone.timer timers (the Clock app).
 */
object LiveRank {
    const val SELF = "phone"  // the phone's Send device id (SendHub.SELF)
    private const val URGENT_SECS = 60L

    fun skip(a: Activity, self: String = SELF): Boolean =
        // its own transfers (either end) already have the Send notification; its own timers the Clock app's
        (a.kind == "transfer" && (self in a.devices || a.device == self)) || (a.kind == "timer" && a.device == self)

    private fun rank(a: Activity, nowSec: Long): Int = when {
        a.kind == "timer" && a.endsAt != null && a.endsAt - nowSec < URGENT_SECS -> 0
        a.kind == "transfer" -> 1
        a.kind == "timer" -> 2
        a.kind == "job" -> 3
        else -> 4
    }

    fun pick(items: List<Activity>, nowSec: Long, self: String = SELF): Activity? =
        items.filter { !skip(it, self) }
            .sortedWith(compareBy<Activity> { rank(it, nowSec) }.thenByDescending { it.startedAt })
            .firstOrNull()

    /** Seconds until a qualifying timer becomes urgent (so the ranking can be redone), or null. */
    fun secsUntilReRank(items: List<Activity>, nowSec: Long, self: String = SELF): Long? =
        items.filter { !skip(it, self) && it.kind == "timer" && it.endsAt != null && it.endsAt - nowSec >= URGENT_SECS }
            .minOfOrNull { it.endsAt!! - URGENT_SECS - nowSec }

    /** The Helios page a tap opens: transfers page, jobs on the map, everything else Helios home. */
    fun route(a: Activity): String = when (a.kind) {
        "transfer" -> "/helios/?app=1#transfers"
        "job" -> "/helios/?app=1#map"
        else -> "/helios/?app=1"
    }
}
