package dev.argus.companion.send

import android.content.Context
import android.content.Intent
import android.graphics.drawable.Icon
import android.content.pm.ShortcutInfo
import android.content.pm.ShortcutManager
import android.net.ConnectivityManager
import android.net.Uri
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.provider.OpenableColumns
import android.util.Log
import dev.argus.companion.Api
import dev.argus.companion.Pause
import dev.argus.companion.Prefs
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.net.Inet4Address
import java.util.UUID

/** Owns the Send parts inside WorkerService: receiver, sender, Argus pairing loop, NSD, share shortcuts. */
object SendHub {
    const val SELF = "phone"
    private val liveExec = java.util.concurrent.Executors.newSingleThreadExecutor()

    /** Live progress to Argus (Helios, the island and the PC show it); the reply may ask to cancel. Best effort. */
    fun reportLive(ctx: Context, item: JSONObject, onCancel: (String) -> Unit) {
        liveExec.execute {
            try {
                val p = Prefs(ctx)
                if (!p.ready) return@execute
                val r = Api(p.url, p.token).post("/send/live", JSONObject().put("device", SELF).put("items", JSONArray().put(item)), 4_000)
                if (r.status == 200) r.json().optJSONArray("cancel")?.let { a -> for (i in 0 until a.length()) onCancel(a.getString(i)) }
            } catch (_: Exception) { /* Argus out of reach: the transfer goes on */ }
        }
    }
    const val EXTRA_TARGET = "dev.argus.companion.SEND_TARGET"

    @Volatile private var engine: SendEngine? = null
    private var receiver: SendReceiver? = null
    private var nsd: NsdManager? = null
    private var reg: NsdManager.RegistrationListener? = null
    private var disc: NsdManager.DiscoveryListener? = null
    @Volatile private var running = false
    @Volatile private var argusUp = false
    private val argusPause = Pause()
    private lateinit var book: DeviceBook

    fun kick() { engine?.kick() }
    fun kickArgus() { argusPause.kick() }
    fun cancel(id: String) { engine?.cancel(id) }

    @Synchronized fun start(ctx0: Context) {
        if (running) { kick(); return }
        val ctx = ctx0.applicationContext
        running = true
        SendNotes.channels(ctx)
        val keys = DeviceKeys(ctx); book = DeviceBook(ctx); val q = SendQueue.get(ctx)
        receiver = SendReceiver(ctx, SELF, keys, book, q).also { runCatching { it.start() }.onFailure { e -> Log.w("argus-send", "port ${SendConst.PORT}", e) } }
        engine = SendEngine(ctx, SELF, keys, book, q) { argusUp }.also { it.start() }
        publishShortcuts(ctx)
        announce(ctx)
        Thread({ argusLoop(ctx, keys, q) }, "argus-send-argus").apply { isDaemon = true; start() }
    }

    @Synchronized fun stop() {
        running = false
        engine?.stop(); engine = null
        receiver?.stop(); receiver = null
        runCatching { reg?.let { nsd?.unregisterService(it) } }; runCatching { disc?.let { nsd?.stopServiceDiscovery(it) } }
        argusPause.kick()
    }

    // ---------------------------------------------------------------- pairing with Argus, fresh addresses, outbox
    private fun ownLan(ctx: Context): List<String> = try {
        val cm = ctx.getSystemService(ConnectivityManager::class.java)
        val lp = cm.getLinkProperties(cm.activeNetwork)
        lp?.linkAddresses?.map { it.address }?.filterIsInstance<Inet4Address>()?.filter { !it.isLoopbackAddress }?.map { it.hostAddress + ":" + SendConst.PORT } ?: emptyList()
    } catch (e: Exception) { emptyList() }

    private fun argusLoop(ctx: Context, keys: DeviceKeys, q: SendQueue) {
        while (running) {
            try {
                val prefs = Prefs(ctx)
                if (prefs.ready) {
                    val api = Api(prefs.url, prefs.token)
                    val r = api.post("/send/devices", JSONObject().put("id", SELF).put("name", "my phone").put("pubkey", keys.pubB64)
                        .put("lan", JSONArray(ownLan(ctx))))
                    if (r.status in 200..299) {
                        val g = api.call("GET", "/send/devices")
                        if (g.status == 200) book.merge(JSONArray(g.body), SELF)
                        for ((eid, json) in q.unsent()) {
                            val e = JSONObject(json).put("eid", eid).put("device", SELF)
                            if (api.post("/send/events", e).status in 200..299) q.sentEvent(eid)
                        }
                        if (!argusUp) { argusUp = true; kick() }  // Argus reachable again is a retry trigger
                        publishShortcuts(ctx)
                    } else argusUp = false
                }
            } catch (e: Exception) { argusUp = false }
            try { argusPause.sleep(30_000) } catch (_: InterruptedException) { return }  // re-register every 30 s
        }
    }

    // ---------------------------------------------------------------- NSD: announce ourselves, find the others
    private fun announce(ctx: Context) {
        try {
            val m = ctx.getSystemService(Context.NSD_SERVICE) as NsdManager; nsd = m
            val info = NsdServiceInfo().apply { serviceName = "argus-send-$SELF"; serviceType = SendConst.SERVICE_TYPE; port = SendConst.PORT; setAttribute("id", SELF) }
            reg = object : NsdManager.RegistrationListener {
                override fun onServiceRegistered(i: NsdServiceInfo) {}
                override fun onRegistrationFailed(i: NsdServiceInfo, e: Int) {}
                override fun onServiceUnregistered(i: NsdServiceInfo) {}
                override fun onUnregistrationFailed(i: NsdServiceInfo, e: Int) {}
            }
            m.registerService(info, NsdManager.PROTOCOL_DNS_SD, reg)
            disc = object : NsdManager.DiscoveryListener {
                override fun onDiscoveryStarted(t: String) {}
                override fun onDiscoveryStopped(t: String) {}
                override fun onStartDiscoveryFailed(t: String, e: Int) {}
                override fun onStopDiscoveryFailed(t: String, e: Int) {}
                override fun onServiceLost(i: NsdServiceInfo) {}
                @Suppress("DEPRECATION")
                override fun onServiceFound(i: NsdServiceInfo) {
                    if (i.serviceName.endsWith(SELF)) return
                    m.resolveService(i, object : NsdManager.ResolveListener {
                        override fun onResolveFailed(s: NsdServiceInfo, e: Int) {}
                        override fun onServiceResolved(s: NsdServiceInfo) {
                            val id = s.attributes["id"]?.let { String(it) } ?: return
                            val ip = s.host?.hostAddress ?: return
                            book.discovered = book.discovered + (id to "$ip:${s.port}")
                            kick()  // the target announced itself: retry now
                        }
                    })
                }
            }
            m.discoverServices(SendConst.SERVICE_TYPE, NsdManager.PROTOCOL_DNS_SD, disc)
        } catch (e: Exception) { Log.w("argus-send", "nsd", e) }
    }

    // ---------------------------------------------------------------- direct-share targets
    private fun publishShortcuts(ctx: Context) {
        try {
            val sm = ctx.getSystemService(ShortcutManager::class.java)
            val list = listOf("calypso" to "Send to Calypso", "pc" to "Send to PC").map { (id, label) ->
                ShortcutInfo.Builder(ctx, "send-$id").setShortLabel(label).setLongLabel(label)
                    .setIcon(Icon.createWithResource(ctx, dev.argus.companion.R.mipmap.ic_launcher))
                    .setCategories(setOf("dev.argus.companion.category.SEND_TARGET"))
                    .setIntent(Intent(Intent.ACTION_SEND).setClassName(ctx, "dev.argus.companion.send.SendShareActivity").putExtra(EXTRA_TARGET, id))
                    .build()
            }
            val others = sm.dynamicShortcuts.filter { !it.id.startsWith("send-") }
            sm.dynamicShortcuts = others + list
        } catch (e: Exception) { Log.w("argus-send", "shortcuts", e) }
    }

    // ---------------------------------------------------------------- queueing a share (called by the share activity)
    fun enqueue(ctx: Context, target: String, uris: List<Uri>): Int {
        val files = ArrayList<FileRow>()
        val id = UUID.randomUUID().toString()
        for (u in uris) {
            var name = u.lastPathSegment?.substringAfterLast('/') ?: "file"; var size = -1L
            try {
                ctx.contentResolver.query(u, arrayOf(OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE), null, null, null)?.use {
                    if (it.moveToFirst()) { it.getString(0)?.let { n -> name = n }; if (!it.isNull(1)) size = it.getLong(1) }
                }
            } catch (_: Exception) {}
            var uri = u.toString()
            if (size in 0..(64L shl 20)) {  // small files are kept here so the sender doesn't depend on the other app's permission lasting
                try {
                    val copy = File(ctx.filesDir, "outbox/$id/${files.size}").apply { parentFile?.mkdirs() }
                    ctx.contentResolver.openInputStream(u)!!.use { i -> copy.outputStream().use { i.copyTo(it) } }
                    uri = Uri.fromFile(copy).toString(); size = copy.length()
                } catch (_: Exception) {}
            }
            if (size < 0) size = try { ctx.contentResolver.openFileDescriptor(u, "r")?.use { it.statSize } ?: 0 } catch (_: Exception) { 0 }
            files += FileRow(files.size, uri, name, size, System.currentTimeMillis(), "")
        }
        if (files.isEmpty()) return 0
        SendQueue.get(ctx).add(id, target, UUID.randomUUID().toString(), files)  // written before any byte is sent
        ctx.getSharedPreferences("argus", Context.MODE_PRIVATE).edit().putString("send_last", target).apply()
        return files.size
    }
}
