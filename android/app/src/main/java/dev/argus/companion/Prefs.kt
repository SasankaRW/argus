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

    val ready: Boolean get() = url.startsWith("http://") || url.startsWith("https://")
}
