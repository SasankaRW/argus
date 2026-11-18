package dev.argus.companion.send

import android.content.Context
import android.net.ConnectivityManager
import android.net.Uri
import android.util.Log
import dev.argus.companion.Pause
import org.json.JSONArray
import org.json.JSONObject
import java.io.FileInputStream
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import java.security.MessageDigest
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.ConcurrentLinkedQueue
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicLong
import java.util.concurrent.atomic.AtomicReference

/** An authenticated session with one receiver over one path (LAN or Tailscale). */
class Link(val kind: String, val base: String, val sid: String, val key: ByteArray, val proof: String) {
    val seq = AtomicLong(0)
    private val born = System.currentTimeMillis()
    fun stale() = System.currentTimeMillis() - born > SendConst.SESSION_MS - 60_000
}

class Reply(val status: Int, val body: String) { fun json() = if (body.isBlank()) JSONObject() else JSONObject(body) }

/** The sender: walks the queue, picks LAN or Tailscale, sends 8 MiB chunks four at a time, resumes, verifies. */
class SendEngine(private val ctx: Context, private val self: String, private val keys: DeviceKeys,
                 private val book: DeviceBook, private val queue: SendQueue, private val argusUp: () -> Boolean) {
    private val pause = Pause()
    private val pool = Executors.newCachedThreadPool()
    private val cancelled = ConcurrentHashMap.newKeySet<String>()
    @Volatile private var running = false
    private var thread: Thread? = null

    fun start() {
        if (running) return
        running = true
        queue.recover()
        thread = Thread({ loop() }, "argus-send").apply { isDaemon = true; start() }
    }

    fun stop() { running = false; thread?.interrupt(); pool.shutdownNow() }
    fun kick() = pause.kick()
    fun cancel(id: String) { cancelled += id }

    private fun loop() {
        var first = true
        while (running) {
            try {
                OutboxCleanup.maybeClean(ctx, force = first); first = false
                for (s in queue.shares(SendState.QUEUED, SendState.HELD)) { if (!running) return; process(s) }
                pause.sleep(30_000)  // the 30 s timer; network changes, NSD and Argus coming back kick() earlier
            } catch (e: InterruptedException) { return } catch (e: Exception) { Log.w("argus-send", "pass failed", e); try { pause.sleep(10_000) } catch (_: InterruptedException) { return } }
        }
    }

    // ---------------------------------------------------------------- http
    private fun raw(url: String, method: String, headers: Map<String, String>, body: ByteArray?, timeoutMs: Int): Reply {
        val c = URL(url).openConnection() as HttpURLConnection
        try {
            c.requestMethod = method; c.connectTimeout = timeoutMs; c.readTimeout = if (body != null && body.size > 65536) 120_000 else maxOf(timeoutMs, 15_000)
            headers.forEach { (k, v) -> c.setRequestProperty(k, v) }
            if (body != null) { c.doOutput = true; c.setFixedLengthStreamingMode(body.size); c.outputStream.use { it.write(body) } }
            val code = c.responseCode
            val text = (if (code >= 400) c.errorStream else c.inputStream)?.bufferedReader()?.use { it.readText() } ?: ""
            return Reply(code, text)
        } finally { c.disconnect() }
    }

    private fun hello(peer: Peer, base: String, kind: String): Link? = try {
        val nonceC = SendCrypto.random(32); val ephPriv = SendCrypto.random(32); val ephC = SendCrypto.xPublic(ephPriv)
        val r = raw("$base/send/hello", "POST", mapOf("Content-Type" to "application/json"),
            JSONObject().put("from", self).put("nonce_c", b64(nonceC)).put("eph_c", b64(ephC)).toString().toByteArray(), 3000)
        if (r.status != 200) null else {
            val j = r.json(); val nonceS = unb64(j.getString("nonce_s")); val ephS = unb64(j.getString("eph_s"))
            if (!SendCrypto.verify(peer.pub, nonceC + ephS, unb64(j.getString("sig")))) null  // not the device we paired with
            else Link(kind, base, j.getString("sid"), SendCrypto.sessionKey(ephPriv, ephS, nonceC, nonceS, keys.pub, peer.pub), b64(keys.sign(nonceS + ephC)))
        }
    } catch (e: Exception) { null }

    private fun lanBases(peer: Peer): List<String> = (peer.lan + listOfNotNull(book.discovered[peer.id])).distinct().map { "http://$it" }
    private fun tsBase(peer: Peer): String? = peer.tailscale?.let { if (it.contains(":")) "http://$it" else "http://$it:${SendConst.PORT}" }

    /** LAN address, mDNS address and Tailscale name at the same time; the first good LAN answer wins, else Tailscale. */
    private fun connect(peer: Peer, lanOnly: Boolean = false): Link? {
        val lan = lanBases(peer).map { b -> pool.submit<Link?> { hello(peer, b, "lan") } }
        val ts = if (lanOnly) null else tsBase(peer)?.let { b -> pool.submit<Link?> { hello(peer, b, "tailscale") } }
        val end = System.currentTimeMillis() + 5000
        while (System.currentTimeMillis() < end) {
            lan.firstOrNull { it.isDone && it.get() != null }?.let { return it.get() }
            if (lan.all { it.isDone }) { if (ts == null) return null; if (ts.isDone) return ts.get() }
            Thread.sleep(40)
        }
        return ts?.takeIf { it.isDone }?.get()
    }

    private fun call(l: Link, method: String, path: String, query: String, body: ByteArray?, chunkSha: String?): Reply {
        val seq = l.seq.incrementAndGet()
        val sha = chunkSha ?: sha256Hex(body ?: ByteArray(0))
        val h = hashMapOf("X-Send-Session" to l.sid, "X-Send-Seq" to seq.toString(), "X-Send-Proof" to l.proof,
            "X-Send-Mac" to SendCrypto.mac(l.key, method, path, query, sha, seq))
        if (chunkSha != null) h["X-Chunk-Sha256"] = chunkSha
        if (body != null && chunkSha == null) h["Content-Type"] = "application/json"
        return raw(l.base + path + (if (query.isEmpty()) "" else "?$query"), method, h, body, 5000)
    }

    /** One request on the current link; a new hello first when the session is old or the receiver forgot it. */
    private fun req(ref: AtomicReference<Link>, peer: Peer, method: String, path: String, query: String = "",
                    body: ByteArray? = null, chunkSha: String? = null): Reply {
        var l = ref.get()
        if (l.stale()) hello(peer, l.base, l.kind)?.let { if (ref.compareAndSet(l, it)) l = it else l = ref.get() }
        var r = call(l, method, path, query, body, chunkSha)
        if (r.status == 401) hello(peer, l.base, l.kind)?.let { ref.compareAndSet(l, it); r = call(it, method, path, query, body, chunkSha) }
        return r
    }

    // ---------------------------------------------------------------- one share
    private fun metered() = ctx.getSystemService(ConnectivityManager::class.java).isActiveNetworkMetered

    private fun fail(s: ShareRow, peer: String, why: String) {
        queue.move(s.id, SendState.FAILED, why); SendNotes.failed(ctx, s.id, peer, why)
        queue.event(UUID.randomUUID().toString(), JSONObject().put("kind", "failed").put("device", self).put("peer", peer).put("detail", why).put("offer_id", s.offerId))
    }

    private fun waiting(s: ShareRow, peerName: String, why: String, wifi: Boolean = false) {
        queue.move(s.id, SendState.QUEUED, why); SendNotes.waiting(ctx, s.id, peerName, why, wifi)
    }

    private fun sizeOf(uri: String): Long = try { ctx.contentResolver.openFileDescriptor(Uri.parse(uri), "r")?.use { it.statSize } ?: -1 } catch (e: Exception) { -2 }

    private fun process(s: ShareRow) {
        val peer = book.get(s.target) ?: return fail(s, s.target, "This device is not paired yet.")
        val name = peer.name
        if (cancelled.remove(s.id)) { queue.move(s.id, SendState.CANCELLED); return }
        if (Transitions.waitsForWifi(s.total, metered(), s.sendAnyway)) return waiting(s, name, "Over 200 MB waits for Wi-Fi.", true)
        if (System.currentTimeMillis() - s.created > 7 * 86_400_000L && queue.markAsked(s.id)) SendNotes.keepWaiting(ctx, s.id, name)
        for (f in s.files) {  // a changed or missing source is reported, not sent
            val now = sizeOf(f.uri)
            if (now == -2L || (now >= 0 && now != f.size)) return fail(s, name, "${f.rel} changed or was deleted.")
        }
        for (f in s.files) if (f.sha.isEmpty()) { f.sha = sha256Uri(f.uri); queue.setSha(s.id, f.n, f.sha) }
        var link = connect(peer)
        var via = peer
        var hold = false
        if (link == null) {
            val cal = book.get("calypso")
            if (s.state == SendState.HELD) return  // already with Calypso; wait for the target
            if (cal != null && s.target != "calypso" && argusUp()) { link = connect(cal); via = cal; hold = link != null }
            if (link == null) return waiting(s, name, "It will go as soon as it is back.")
        }
        transfer(s, peer, via, link!!, hold)
    }

    private fun sha256Uri(uri: String): String {
        val md = MessageDigest.getInstance("SHA-256"); val buf = ByteArray(1 shl 20)
        ctx.contentResolver.openInputStream(Uri.parse(uri))!!.use { while (true) { val r = it.read(buf); if (r < 0) break; md.update(buf, 0, r) } }
        return hex(md.digest())
    }

    private fun readChunk(uri: String, off: Long, len: Int): ByteArray {
        ctx.contentResolver.openFileDescriptor(Uri.parse(uri), "r")!!.use { fd ->
            val ch = FileInputStream(fd.fileDescriptor).channel; ch.position(off)
            val b = java.nio.ByteBuffer.allocate(len)
            while (b.hasRemaining()) if (ch.read(b) < 0) throw IOException("source ended early")
            return b.array()
        }
    }

    private fun transfer(s: ShareRow, target: Peer, via: Peer, first: Link, hold: Boolean) {
        val wasHeld = s.state == SendState.HELD
        val ref = AtomicReference(first)
        val path = "/send/offers/${s.offerId}"
        val offer = JSONObject().put("offer_id", s.offerId).put("from", self).put("total", s.total).put("chunk", SendConst.CHUNK)
            .put("files", JSONArray().also { a -> s.files.forEach { a.put(JSONObject().put("n", it.n).put("rel_path", it.rel).put("size", it.size).put("mtime", it.mtime / 1000).put("sha256", it.sha)) } })
        if (hold) offer.put("hold_for", target.id)
        queue.move(s.id, SendState.SENDING)
        val t0 = System.currentTimeMillis()
        val sentBytes = AtomicLong(0)
        try {
            var r = req(ref, via, "POST", "/send/offers", body = offer.toString().toByteArray())
            repeat(3) {
                if (r.status == 507) return waiting(s, via.name, "${via.name} needs ${SendNotes.human(r.json().optLong("missing_bytes"))} more free space.")
                if (r.status == 403) return fail(s, via.name, "${via.name} does not recognise this phone.")
                if (r.status != 200) throw IOException("offer: HTTP ${r.status}")
                val j = r.json(); val have = j.getJSONObject("have")
                val done = (0 until j.getJSONArray("done").length()).map { j.getJSONArray("done").getInt(it) }.toSet()
                val tasks = ConcurrentLinkedQueue<Pair<FileRow, Int>>()
                var already = 0L
                for (f in s.files) {
                    val h = if (f.n in done) (0 until Chunks.count(f.size)).toList() else have.optJSONArray(f.n.toString())?.let { a -> (0 until a.length()).map { a.getInt(it) } } ?: emptyList()
                    already += Chunks.bytesHave(f.size, h)
                    Chunks.pending(f.size, h).forEach { tasks.add(f to it) }
                }
                sentBytes.set(already)
                val err = upload(s, via, target, ref, tasks, sentBytes, hold)
                if (cancelled.remove(s.id)) { queue.move(s.id, SendState.CANCELLED); SendNotes.clear(ctx, s.id); live(s, via.id, sentBytes.get(), "cancelled"); return }
                if (err != null) { live(s, via.id, sentBytes.get(), "failed"); return waiting(s, target.name, err) }
                queue.move(s.id, SendState.VERIFYING)
                r = req(ref, via, "POST", "$path/done", body = ByteArray(0))
                if (r.status == 200) { finished(s, target, via, hold, wasHeld, t0); return }
                if (r.status != 409) throw IOException("done: HTTP ${r.status}")
                queue.move(s.id, SendState.SENDING)  // some chunks did not verify: ask what is held now and send those again
                r = req(ref, via, "GET", path)
            }
            waiting(s, target.name, "A file would not verify; trying again later.")
        } catch (e: IOException) { Log.w("argus-send", "transfer", e); waiting(s, target.name, "Connection lost; it will resume.") }
    }

    /** Sends the chunks four at a time; switches path mid-transfer. Returns a reason when it could not finish. */
    private fun upload(s: ShareRow, via: Peer, target: Peer, ref: AtomicReference<Link>, tasks: ConcurrentLinkedQueue<Pair<FileRow, Int>>,
                       sent: AtomicLong, hold: Boolean): String? {
        val stop = AtomicReference<String?>(null)
        val live = AtomicInteger(0)
        val tries = ConcurrentHashMap<String, Int>()
        val workers = (1..SendConst.PARALLEL).map {
            live.incrementAndGet()
            pool.submit {
                try {
                    while (stop.get() == null && s.id !in cancelled) {
                        val (f, i) = tasks.poll() ?: break
                        try {
                            val body = readChunk(f.uri, Chunks.offset(i), Chunks.length(f.size, i))
                            val r = req(ref, via, "PUT", "/send/offers/${s.offerId}/files/${f.n}", "chunk=$i", body, sha256Hex(body))
                            if (r.status != 200) throw IOException("chunk: HTTP ${r.status}")
                            sent.addAndGet(body.size.toLong())
                        } catch (e: IOException) {
                            val k = "${f.n}/$i"; val n = (tries[k] ?: 0) + 1; tries[k] = n
                            val dropped = ref.get()
                            val other = if (n <= 3) connect(via).takeIf { it != null } else null  // LAN failed: Tailscale, and back
                            if (other == null) stop.compareAndSet(null, "${target.name} stopped answering; it will resume.")
                            else { ref.compareAndSet(dropped, other); tasks.add(f to i) }
                        }
                    }
                } finally { live.decrementAndGet() }
            }
        }
        var tick = 0; var last = sent.get(); var lastAt = System.currentTimeMillis()
        while (live.get() > 0 || workers.any { !it.isDone }) {
            Thread.sleep(500)
            tick++
            if (tick % 2 == 0) {
                val now = System.currentTimeMillis(); val b = sent.get()
                SendNotes.progress(ctx, s.id, target.name, b, s.total, (b - last) * 1000 / maxOf(1, now - lastAt), target.id); last = b; lastAt = now
            }
            run {  // twice a second: live progress for Argus (and a cancel from Helios or the island)
                val b = sent.get(); val now = System.currentTimeMillis()
                live(s, via.id, b, "moving", (b - last) * 1000 / maxOf(1, now - lastAt))
            }
            // on Tailscale: try the LAN every 5 s; on success the chunks in flight finish and the rest go over the LAN
            if (tick % 10 == 0 && ref.get().kind == "tailscale") connect(via, lanOnly = true)?.let { ref.set(it) }
        }
        return stop.get()
    }

    /** This share's live progress, for Argus; a cancel asked there stops the upload like the notification's Cancel. */
    private fun live(s: ShareRow, to: String, done: Long, state: String, bps: Long = 0) {
        val eta: Any = if (bps > 0 && state == "moving" && s.total > done) (s.total - done).toDouble() / bps else JSONObject.NULL
        val o = JSONObject().put("offer_id", s.offerId).put("leg", "send").put("from", self).put("to", to)
            .put("label", s.files.first().rel.substringBefore('/') + if (s.files.size > 1) " +${s.files.size - 1}" else "")
            .put("files", s.files.size).put("total", s.total).put("done", done).put("state", state).put("speed", bps).put("eta", eta)
        SendHub.reportLive(ctx, o) { oid -> if (oid == s.offerId) cancelled += s.id }
    }

    private fun finished(s: ShareRow, target: Peer, via: Peer, hold: Boolean, wasHeld: Boolean, t0: Long) {
        live(s, via.id, s.total, "done")
        runCatching {  // Helios in the app can open what was shared, later, Argus or not
            queue.remember("sent", s.offerId, s.files.first().rel + if (s.files.size > 1) " +${s.files.size - 1}" else "", target.name,
                s.files.map { SendQueue.MineFile(it.n, it.rel, it.uri, it.size) })
        }
        val ev = JSONObject().put("device", self).put("peer", target.name).put("bytes", s.total).put("offer_id", s.offerId)
            .put("label", s.files.first().rel + if (s.files.size > 1) " +${s.files.size - 1}" else "")
        if (hold) {
            queue.move(s.id, SendState.HELD, "With ${via.name}, waiting for ${target.name}")
            SendNotes.waiting(ctx, s.id, target.name, "Handed to ${via.name}; it will deliver when ${target.name} is back.")
            queue.event(UUID.randomUUID().toString(), ev.put("kind", "held"))
        } else {
            queue.move(s.id, SendState.DONE); queue.remove(s.id)  // the entry goes only after the receiver's 200
            SendNotes.sent(ctx, s.id, target.name)
            queue.event(UUID.randomUUID().toString(), ev.put("kind", "sent"))
            if (wasHeld) book.get("calypso")?.let { cal -> connect(cal)?.let { l -> runCatching { req(AtomicReference(l), cal, "DELETE", "/send/holding/${s.offerId}") } } }
        }
        SendHub.kickArgus()
    }
}
