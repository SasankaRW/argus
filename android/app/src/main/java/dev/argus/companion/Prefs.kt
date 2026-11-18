package dev.argus.companion

import android.content.Context

/** Where Argus is and the worker token, kept on the phone only. */
class Prefs(context: Context) {
    private val sp = context.getSharedPreferences("argus", Context.MODE_PRIVATE)

    var url: String
        get() = sp.getString("url", "") ?: ""
        set(v) = sp.edit().putString("url", v.trim().trimEnd('/')).apply()

    var token: String
        get() = sp.getString("token", "") ?: ""
        set(v) = sp.edit().putString("token", v.trim()).apply()

    var worker: Boolean
        get() = sp.getBoolean("worker", true)
        set(v) = sp.edit().putBoolean("worker", v).apply()

    /** Clipboard and link sync with the PC (off: nothing is read or sent, incoming clips are refused). */
    var clipSync: Boolean
        get() = sp.getBoolean("clip_sync", true)
        set(v) = sp.edit().putBoolean("clip_sync", v).apply()

    /** The device clips go to; the PC unless changed. */
    var clipPeer: String
        get() = sp.getString("clip_peer", "pc") ?: "pc"
        set(v) = sp.edit().putString("clip_peer", v.trim().ifEmpty { "pc" }).apply()

    val ready: Boolean get() = url.startsWith("http://") || url.startsWith("https://")
}
