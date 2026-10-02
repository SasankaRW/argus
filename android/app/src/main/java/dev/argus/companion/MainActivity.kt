package dev.argus.companion

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.app.NotificationManager
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import android.text.InputType
import android.view.Gravity
import android.view.ViewGroup.LayoutParams.MATCH_PARENT
import android.view.ViewGroup.LayoutParams.WRAP_CONTENT
import android.webkit.PermissionRequest
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import org.json.JSONObject
import kotlin.concurrent.thread

/**
 * Helios (the same web UI as on the PC) full screen, plus a small setup page: where Argus is, the token, and the
 * phone permissions Ari's phone tools need. The setup page opens on first start and from the app's long-press menu.
 */
class MainActivity : Activity() {

    private var web: WebView? = null
    private var onSetup = false
    private val bg = Color.parseColor("#07090C")
    private val ink = Color.parseColor("#E4E7EE")
    private val dim = Color.parseColor("#7D8699")

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        WorkerService.channels(this)
        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
        }
        route(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        route(intent)
    }

    private fun route(intent: Intent?) {
        val prefs = Prefs(this)
        if (!prefs.ready || intent?.action == "dev.argus.companion.SETTINGS") setup()
        else helios(intent?.getStringExtra("path"))  // a notification's tap opens its page
    }

    // ------------------------------------------------------------------ Helios

    @SuppressLint("SetJavaScriptEnabled")
    private fun helios(path: String? = null) {
        onSetup = false
        val prefs = Prefs(this)
        WorkerService.start(this)
        val w = web ?: WebView(this).also { web = it }
        w.setBackgroundColor(bg)
        w.settings.javaScriptEnabled = true
        w.settings.domStorageEnabled = true
        w.settings.mediaPlaybackRequiresUserGesture = false
        w.webViewClient = object : WebViewClient() {
            // Helios stays in the app; any other link opens in the browser
            override fun shouldOverrideUrlLoading(view: WebView, req: WebResourceRequest): Boolean {
                val inside = req.url.toString().startsWith(prefs.url)
                if (!inside) startActivity(Intent(Intent.ACTION_VIEW, req.url))
                return !inside
            }
        }
        w.webChromeClient = object : WebChromeClient() {
            // Ari's microphone in Helios
            override fun onPermissionRequest(request: PermissionRequest) {
                if (request.origin.toString().startsWith(prefs.url) &&
                    request.resources.contains(PermissionRequest.RESOURCE_AUDIO_CAPTURE)) {
                    if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
                        request.grant(arrayOf(PermissionRequest.RESOURCE_AUDIO_CAPTURE))
                    } else {
                        requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), 2)
                        request.deny()
                    }
                } else {
                    request.deny()
                }
            }
        }
        val target = path?.trim()?.takeIf { it.isNotEmpty() }?.let { if (it.startsWith("/")) prefs.url + it else it }
        when {
            target != null && target.startsWith(prefs.url) -> w.loadUrl(target)
            target != null && (target.startsWith("http://") || target.startsWith("https://")) ->
                startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(target)))
            w.url == null -> w.loadUrl("${prefs.url}/helios/?app=1&token=${Uri.encode(prefs.token)}")
        }
        setContentView(w)
    }

    @Deprecated("Activity.onBackPressed is fine here: no AndroidX")
    override fun onBackPressed() {
        val w = web
        if (w != null && w.parent != null && w.canGoBack()) w.goBack() else super.onBackPressed()
    }

    // ------------------------------------------------------------------ setup

    private fun setup() {
        onSetup = true
        val prefs = Prefs(this)
        val pad = dp(20)
        val col = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, dp(36), pad, pad)
            setBackgroundColor(bg)
        }
        col.addView(label("Argus", 26f, ink, bold = true))
        col.addView(label("Connect this phone to your Argus. Use its Tailscale address, e.g. http://sas-pc:8600 " +
            "(later the laptop), and the worker token from Argus's .env (ARGUS_WORKER_TOKEN).", 14f, dim))
        val url = field("Argus address", prefs.url, InputType.TYPE_TEXT_VARIATION_URI)
        val token = field("Worker token", prefs.token, InputType.TYPE_TEXT_VARIATION_PASSWORD)
        col.addView(url)
        col.addView(token)
        val status = label("", 13f, dim)
        col.addView(button("Connect", primary = true) {
            prefs.url = url.text.toString()
            prefs.token = token.text.toString()
            if (!prefs.ready) {
                status.text = "The address must start with http:// or https://"
                return@button
            }
            status.text = "Checking…"
            thread {
                val msg = try {
                    val r = Api(prefs.url, prefs.token).call("GET", "/workers")
                    when (r.status) {
                        200 -> null
                        401, 403 -> "Argus answered, but refused the token."
                        else -> "Argus answered HTTP ${r.status}."
                    }
                } catch (e: Exception) {
                    "Can't reach ${prefs.url} (is Tailscale on?)"
                }
                runOnUiThread {
                    if (msg == null) {
                        WorkerService.stop(this)
                        web?.let { it.loadUrl("about:blank"); it.clearHistory() }
                        web = null
                        helios()
                    } else {
                        status.text = msg
                    }
                }
            }
        })
        col.addView(status)
        col.addView(label("Notifications and staying connected", 17f, ink, bold = true, top = dp(28)))
        col.addView(label("Argus collects your notifications itself, so they arrive with this app closed. Android " +
            "must let it keep running in the background.", 13f, dim))
        val notifOk = Build.VERSION.SDK_INT < 33 ||
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED
        col.addView(button(check(notifOk) + "Show notifications") {
            if (Build.VERSION.SDK_INT >= 33) requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
        })
        val pm = getSystemService(PowerManager::class.java)
        col.addView(button(check(pm.isIgnoringBatteryOptimizations(packageName)) + "Run in the background (no battery limit)") {
            startActivity(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$packageName")))
        })
        col.addView(button("Send me a test notification") {
            thread {
                val ok = try { Api(prefs.url, prefs.token).post("/outbox/test", JSONObject()).status in 200..299 } catch (e: Exception) { false }
                runOnUiThread { status.text = if (ok) "Sent: it should appear within a few seconds." else "Couldn't reach Argus." }
            }
        })
        col.addView(label("Phone control", 17f, ink, bold = true, top = dp(28)))
        col.addView(label("So Ari can do things on this phone. Each opens Android's own setting. The first one lets " +
            "her press Back and Home, lock the screen, read the screen, tap and type, only when you ask her to " +
            "(she asks you first before tapping or typing). If Android greys it out: App info > ⋮ > Allow " +
            "restricted settings, then try again.", 13f, dim))
        col.addView(button(check(ControlService.enabled(this)) + "Phone control (Accessibility > Argus)") {
            startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
        })
        val nm = getSystemService(NotificationManager::class.java)
        col.addView(button(check(nm.isNotificationPolicyAccessGranted) + "Do Not Disturb control") {
            startActivity(Intent(Settings.ACTION_NOTIFICATION_POLICY_ACCESS_SETTINGS))
        })
        col.addView(button(check(Settings.canDrawOverlays(this)) + "Open apps and links (display over other apps)") {
            startActivity(Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, Uri.parse("package:$packageName")))
        })
        col.addView(button(check(checkSelfPermission(Manifest.permission.RECORD_AUDIO) ==
            PackageManager.PERMISSION_GRANTED) + "Microphone (talk to Ari in Helios)") {
            requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), 2)
        })
        col.addView(button((if (prefs.worker) "✓  " else "○  ") + "Let Argus use this phone (phone tools)") {
            prefs.worker = !prefs.worker
            WorkerService.stop(this)
            WorkerService.start(this)  // notifications keep coming either way
            setup()
        })
        setContentView(ScrollView(this).apply { setBackgroundColor(bg); addView(col) })
    }

    override fun onResume() {
        super.onResume()
        if (onSetup) setup()  // back from one of Android's settings: show what is allowed now
    }

    private fun check(ok: Boolean) = if (ok) "✓  " else "○  "

    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()

    private fun label(s: String, size: Float, color: Int, bold: Boolean = false, top: Int = 0) = TextView(this).apply {
        text = s
        textSize = size
        setTextColor(color)
        typeface = if (bold) Typeface.create("sans-serif-medium", Typeface.NORMAL) else Typeface.DEFAULT
        setPadding(0, top, 0, dp(10))
    }

    private fun field(hint: String, value: String, type: Int) = EditText(this).apply {
        this.hint = hint
        setText(value)
        inputType = InputType.TYPE_CLASS_TEXT or type
        setTextColor(ink)
        setHintTextColor(dim)
        isSingleLine = true
        background = GradientDrawable().apply {
            cornerRadius = dp(10).toFloat()
            setColor(Color.parseColor("#0C0F14"))
            setStroke(dp(1), Color.parseColor("#262E3A"))
        }
        setPadding(dp(14), dp(12), dp(14), dp(12))
        layoutParams = LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT).apply { bottomMargin = dp(10) }
    }

    private fun button(s: String, primary: Boolean = false, onClick: () -> Unit) = Button(this).apply {
        text = s
        isAllCaps = false
        gravity = Gravity.CENTER_VERTICAL or Gravity.START
        setTextColor(if (primary) Color.parseColor("#FFBD4A") else ink)
        background = GradientDrawable().apply {
            cornerRadius = dp(10).toFloat()
            setColor(Color.parseColor(if (primary) "#1A150D" else "#0C0F14"))
            setStroke(dp(1), if (primary) Color.argb(110, 245, 165, 36) else Color.parseColor("#262E3A"))
        }
        setPadding(dp(16), 0, dp(16), 0)
        minHeight = dp(48)
        stateListAnimator = null
        layoutParams = LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT).apply { bottomMargin = dp(8) }
        setOnClickListener { onClick() }
    }
}
