package dev.argus.companion

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.content.ComponentName
import android.content.Context
import android.graphics.Path
import android.graphics.Rect
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * Lets Ari use the phone like a person would: press Back / Home, lock it, read what is on the screen, tap a
 * button by its label, type, scroll. It only does something when Ari's phone tools ask for it (the Actions
 * class), and it is off until you switch it on in Android Settings > Accessibility > Argus. It reads no
 * password fields.
 */
class ControlService : AccessibilityService() {

    companion object {
        @Volatile var instance: ControlService? = null

        fun enabled(ctx: Context): Boolean {
            val on = Settings.Secure.getString(ctx.contentResolver, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES)
                ?: return false
            val me = ComponentName(ctx, ControlService::class.java)
            return on.split(':').any { it == me.flattenToString() || it == me.flattenToShortString() }
        }

        fun need(): ControlService = instance ?: throw ActionError(
            "Phone control is off: open the Argus app > Phone control, then turn Argus on in Accessibility")
    }

    override fun onServiceConnected() {
        instance = this
    }

    override fun onUnbind(intent: android.content.Intent?): Boolean {
        instance = null
        return super.onUnbind(intent)
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {}

    override fun onInterrupt() {}

    // ------------------------------------------------------------------ buttons

    fun press(button: String): String {
        val (action, said) = when (button.lowercase().trim()) {
            "back" -> GLOBAL_ACTION_BACK to "back"
            "home" -> GLOBAL_ACTION_HOME to "home"
            "recents", "recent apps", "overview" -> GLOBAL_ACTION_RECENTS to "recent apps"
            "notifications", "notification shade" -> GLOBAL_ACTION_NOTIFICATIONS to "the notification shade"
            "quick_settings", "quick settings" -> GLOBAL_ACTION_QUICK_SETTINGS to "quick settings"
            "screenshot" -> if (Build.VERSION.SDK_INT >= 28) GLOBAL_ACTION_TAKE_SCREENSHOT to "a screenshot"
                            else throw ActionError("screenshots need Android 9 or newer")
            else -> throw ActionError("buttons: back, home, recents, notifications, quick_settings, screenshot")
        }
        if (!performGlobalAction(action)) throw ActionError("Android refused the $said button")
        return said
    }

    fun lock() {
        if (Build.VERSION.SDK_INT < 28) throw ActionError("locking the screen needs Android 9 or newer")
        if (!performGlobalAction(GLOBAL_ACTION_LOCK_SCREEN)) throw ActionError("Android refused to lock the screen")
    }

    // ------------------------------------------------------------------ the screen

    /** The text on the screen (labels, texts, fields), top to bottom, with what can be tapped marked. */
    fun screenText(maxChars: Int = 6000): String {
        val root = rootInActiveWindow ?: throw ActionError("nothing readable is on the screen (is it locked?)")
        val out = StringBuilder()
        out.append("app: ").append(root.packageName ?: "?").append('\n')
        fun walk(n: AccessibilityNodeInfo, depth: Int) {
            if (out.length >= maxChars || depth > 40 || !n.isVisibleToUser) return
            if (!n.isPassword) {
                val t = (n.text ?: n.contentDescription)?.toString()?.trim().orEmpty()
                if (t.isNotEmpty()) {
                    val mark = when {
                        n.isEditable -> " [field]"
                        n.isClickable -> " [tap]"
                        else -> ""
                    }
                    out.append(t.take(300)).append(mark).append('\n')
                }
            }
            for (i in 0 until n.childCount) n.getChild(i)?.let { walk(it, depth + 1) }
        }
        walk(root, 0)
        return out.toString().take(maxChars)
    }

    /** Taps the thing with this label (its button, or the first clickable parent); falls back to its centre. */
    fun tapText(text: String): String {
        val root = rootInActiveWindow ?: throw ActionError("nothing is on the screen to tap (is it locked?)")
        val want = text.trim()
        val hits = root.findAccessibilityNodeInfosByText(want).filter { it.isVisibleToUser && !it.isPassword }
        val hit = hits.firstOrNull { (it.text ?: it.contentDescription)?.toString()?.equals(want, true) == true }
            ?: hits.firstOrNull() ?: throw ActionError("I can't see \"$want\" on the screen")
        var n: AccessibilityNodeInfo? = hit
        while (n != null && !n.isClickable) n = n.parent
        if (n != null && n.performAction(AccessibilityNodeInfo.ACTION_CLICK)) {
            return (hit.text ?: hit.contentDescription)?.toString() ?: want
        }
        val r = Rect()
        hit.getBoundsInScreen(r)
        tapAt(r.centerX().toFloat(), r.centerY().toFloat())
        return (hit.text ?: hit.contentDescription)?.toString() ?: want
    }

    /** x and y as percent of the screen. */
    fun tap(xPct: Int, yPct: Int) {
        val dm = resources.displayMetrics
        tapAt(dm.widthPixels * xPct.coerceIn(0, 100) / 100f, dm.heightPixels * yPct.coerceIn(0, 100) / 100f)
    }

    fun swipe(direction: String) {
        val dm = resources.displayMetrics
        val w = dm.widthPixels.toFloat()
        val h = dm.heightPixels.toFloat()
        // "up" = the content moves up (you read further down): the finger goes from low to high
        val (x0, y0, x1, y1) = when (direction.lowercase().trim()) {
            "up" -> listOf(w / 2, h * 0.70f, w / 2, h * 0.30f)
            "down" -> listOf(w / 2, h * 0.30f, w / 2, h * 0.70f)
            "left" -> listOf(w * 0.80f, h / 2, w * 0.20f, h / 2)
            "right" -> listOf(w * 0.20f, h / 2, w * 0.80f, h / 2)
            else -> throw ActionError("swipe up, down, left or right")
        }
        val p = Path().apply { moveTo(x0, y0); lineTo(x1, y1) }
        gesture(GestureDescription.StrokeDescription(p, 0, 300))
    }

    /** Types into the focused field (or the first field on the screen). Returns what the field says now. */
    fun type(text: String, replace: Boolean): String {
        val root = rootInActiveWindow ?: throw ActionError("nothing is on the screen to type into")
        val field = root.findFocus(AccessibilityNodeInfo.FOCUS_INPUT)?.takeIf { it.isEditable }
            ?: firstEditable(root) ?: throw ActionError("no text field is open on the phone")
        if (field.isPassword) throw ActionError("that is a password field: I won't type there")
        val old = if (field.isShowingHintText) "" else field.text?.toString().orEmpty()
        val now = if (replace) text else old + text
        val args = Bundle().apply { putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, now) }
        if (!field.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)) throw ActionError("the field refused the text")
        return now
    }

    private fun firstEditable(n: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        if (n.isEditable && n.isVisibleToUser) return n
        for (i in 0 until n.childCount) n.getChild(i)?.let { c -> firstEditable(c)?.let { return it } }
        return null
    }

    private fun tapAt(x: Float, y: Float) {
        val p = Path().apply { moveTo(x, y); lineTo(x, y) }
        gesture(GestureDescription.StrokeDescription(p, 0, 60))
    }

    private fun gesture(stroke: GestureDescription.StrokeDescription) {
        val done = CountDownLatch(1)
        var ok = false
        val sent = dispatchGesture(GestureDescription.Builder().addStroke(stroke).build(),
            object : GestureResultCallback() {
                override fun onCompleted(g: GestureDescription?) { ok = true; done.countDown() }
                override fun onCancelled(g: GestureDescription?) { done.countDown() }
            }, null)
        if (!sent || !done.await(4, TimeUnit.SECONDS) || !ok) throw ActionError("the touch didn't go through")
    }
}
