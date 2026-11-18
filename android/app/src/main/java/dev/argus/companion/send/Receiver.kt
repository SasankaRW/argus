package dev.argus.companion.send

import android.content.ContentValues
import android.content.Context
import android.media.MediaScannerConnection
import android.os.Build
import android.os.Environment
import android.os.StatFs
import android.provider.MediaStore
import android.util.Log
import android.webkit.MimeTypeMap
import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedInputStream
import java.io.File
import java.io.InputStream
import java.io.RandomAccessFile
import java.net.ServerSocket
import java.net.Socket
import java.security.MessageDigest
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.Executors

private class Session(val peer: String, val key: ByteArray, val nonceS: ByteArray, val ephC: ByteArray, val expires: Long) {
    @Volatile var proven = false
    val seqs = SeqWindow()
}

private class HttpError(val status: Int, val body: JSONObject) : Exception()

/** The receiver on port 8610: a small hand-written HTTP/1.1 server (no libraries), run inside WorkerService. */
class SendReceiver(private val ctx: Context, private val self: String, private val keys: DeviceKeys,
                   private val book: DeviceBook, private val queue: SendQueue) {
    private val sessions = ConcurrentHashMap<String, Session>()
    private val pool = Executors.newCachedThreadPool()
    private val locks = ConcurrentHashMap<String, Any>()
    @Volatile private var server: ServerSocket? = null
    private val root = File(ctx.getExternalFilesDir(null) ?: ctx.filesDir, "incoming")

    fun start() {
        if (server != null) return
        val s = ServerSocket(SendConst.PORT).also { server = it }
        pool.execute {
            while (!s.isClosed) {
                try { val c = s.accept(); pool.execute { serve(c) } } catch (e: Exception) { if (s.isClosed) return@execute }
            }
        }
    }

    fun stop() { runCatching { server?.close() }; server = null; pool.shutdownNow() }

    // ---------------------------------------------------------------- http
    private class Req(val method: String, val path: String, val query: String, val headers: Map<String, String>, val body: ByteArray)

    private fun serve(sock: Socket) {
        sock.use { s ->
            s.soTimeout = 60_000
            val inp = BufferedInputStream(s.getInputStream(), 65536)
            val out = s.getOutputStream()
            while (true) {
                val req = try { readRequest(inp) ?: return } catch (e: Exception) { return }
                val (code, body) = try { route(req) } catch (e: HttpError) { e.status to e.body } catch (e: Exception) {
                    Log.w("argus-send", "receiver: ${req.method} ${req.path}", e); 500 to JSONObject().put("detail", "server error")
                }
                val bytes = body.toString().toByteArray()
                out.write(("HTTP/1.1 $code OK\r\nContent-Type: application/json\r\nContent-Length: ${bytes.size}\r\nConnection: keep-alive\r\n\r\n").toByteArray())
                out.write(bytes); out.flush()
            }
        }
    }

    private fun line(i: InputStream): String? {
        val sb = StringBuilder()
        while (true) {
            val b = i.read()
            if (b < 0) return if (sb.isEmpty()) null else sb.toString()
            if (b == '\n'.code) return sb.toString().trimEnd('\r')
            if (sb.length > 8192) throw IllegalStateException("line too long")
            sb.append(b.toChar())
        }
    }

    private fun readRequest(i: InputStream): Req? {
        val first = line(i) ?: return null
        val p = first.split(" ")
        if (p.size < 2) return null
        val headers = HashMap<String, String>()
        while (true) { val l = line(i) ?: return null; if (l.isEmpty()) break; val k = l.indexOf(':'); if (k > 0) headers[l.substring(0, k).trim().lowercase()] = l.substring(k + 1).trim() }
        val len = headers["content-length"]?.toIntOrNull() ?: 0
        if (len < 0 || len > 40 * 1024 * 1024) throw IllegalStateException("body too large")
        val body = ByteArray(len); var got = 0
        while (got < len) { val r = i.read(body, got, len - got); if (r < 0) return null; got += r }
        val q = p[1].indexOf('?')
        return Req(p[0], if (q < 0) p[1] else p[1].substring(0, q), if (q < 0) "" else p[1].substring(q + 1), headers, body)
    }

    private fun err(code: Int, text: String, extra: JSONObject = JSONObject()) = HttpError(code, extra.put("detail", text))

    // ---------------------------------------------------------------- auth
    private fun hello(req: Req): Pair<Int, JSONObject> {
        val b = JSONObject(String(req.body))
        val from = b.optString("from")
        val peer = book.get(from) ?: throw err(403, "unknown device")
        val nonceC = try { unb64(b.getString("nonce_c")) } catch (e: Exception) { throw err(400, "bad nonce") }
        val ephC = try { unb64(b.getString("eph_c")) } catch (e: Exception) { throw err(400, "bad key exchange") }
        val ephPriv = SendCrypto.random(32); val ephS = SendCrypto.xPublic(ephPriv)
        val nonceS = SendCrypto.random(32)
        val k = try { SendCrypto.sessionKey(ephPriv, ephC, nonceC, nonceS, peer.pub, keys.pub) } catch (e: Exception) { throw err(400, "bad key exchange") }
        val now = System.currentTimeMillis()
        sessions.entries.removeIf { it.value.expires < now }
        val sid = b64(SendCrypto.random(16)).replace('+', '-').replace('/', '_').trimEnd('=')
        sessions[sid] = Session(from, k, nonceS, ephC, now + SendConst.SESSION_MS)
        return 200 to JSONObject().put("sid", sid).put("nonce_s", b64(nonceS)).put("eph_s", b64(ephS))
            .put("sig", b64(keys.sign(nonceC + ephS)))
    }

    /** Session, proof (first request), seq (never twice) and MAC, in that order; returns the session. */
    private fun check(req: Req, bodySha: String): Session {
        val s = sessions[req.headers["x-send-session"] ?: ""]
        if (s == null || s.expires < System.currentTimeMillis()) throw err(401, "no session: say hello first")
        if (!s.proven) {
            val peer = book.get(s.peer); val proof = req.headers["x-send-proof"]
            val ok = peer != null && proof != null && try { SendCrypto.verify(peer.pub, s.nonceS + s.ephC, unb64(proof)) } catch (e: Exception) { false }
            if (!ok) throw err(403, "key not recognised")
            s.proven = true
        }
        val seq = req.headers["x-send-seq"]?.toLongOrNull() ?: throw err(401, "missing seq")
        val mac = req.headers["x-send-mac"] ?: ""
        if (!SendCrypto.macOk(s.key, req.method, req.path, req.query, bodySha, seq, mac)) throw err(401, "bad MAC")
        if (!s.seqs.accept(seq)) throw err(401, "seq already seen")  // only a request with a valid MAC may use up a number
        return s
    }

    // ---------------------------------------------------------------- routes
    private fun route(req: Req): Pair<Int, JSONObject> {
        if (req.method == "POST" && req.path == "/send/hello") return hello(req)
        val parts = req.path.trim('/').split('/')
        if (parts.size < 2 || parts[0] != "send") throw err(404, "not found")
        val bodySha = if (req.method == "PUT") req.headers["x-chunk-sha256"] ?: "" else sha256Hex(req.body)
        val s = check(req, bodySha)
        return when {
            req.method == "POST" && parts.size == 2 && parts[1] == "offers" -> offer(s, req)
            req.method == "POST" && parts.size == 2 && parts[1] == "clip" -> ClipHub.receive(ctx, s.peer, s.key, req.body)
            parts.size == 3 && parts[1] == "offers" && req.method == "GET" -> reply(findOffer(parts[2]))
            parts.size == 5 && parts[1] == "offers" && parts[3] == "files" && req.method == "PUT" ->
                chunk(parts[2], parts[4].toIntOrNull() ?: throw err(400, "bad file"), req)
            parts.size == 4 && parts[1] == "offers" && parts[3] == "done" && req.method == "POST" -> done(parts[2])
            parts.size == 3 && parts[1] == "holding" && req.method == "DELETE" -> 200 to JSONObject().put("ok", true)
            else -> throw err(404, "not found")
        }
    }

    private val idRe = Regex("^[A-Za-z0-9-]{8,64}$")
    private fun dir(id: String): File { if (!idRe.matches(id)) throw err(400, "bad offer id"); return File(root, id) }

    private fun findOffer(id: String): Pair<File, JSONObject> {
        val d = dir(id); val f = File(d, "offer.json")
        if (!f.exists()) throw err(404, "no such offer")
        return d to JSONObject(f.readText())
    }

    private fun chunksOf(d: File, n: Int): Set<Int> =
        File(d, "$n.chunks").takeIf { it.exists() }?.readLines()?.mapNotNull { it.trim().toIntOrNull() }?.toSet() ?: emptySet()

    private fun folder(offer: JSONObject): String =
        "Download/Argus/From " + clean(book.nameOf(offer.optString("origin").ifBlank { offer.getString("from") })) + "/"

    private fun clean(n: String) = n.replace(Regex("[<>:\"/\\\\|?*\\x00-\\x1f]"), "_").trim(' ', '.').ifEmpty { "_" }

    /** rel_path made safe: no "..", no odd characters; structure kept. */
    private fun safeRel(rel: String): List<String> =
        rel.replace('\\', '/').split('/').filter { it.isNotEmpty() && it != "." && it != ".." }.map { clean(it) }.ifEmpty { throw err(400, "bad path") }

    private fun reply(p: Pair<File, JSONObject>): Pair<Int, JSONObject> {
        val (d, offer) = p
        val have = JSONObject(); val done = JSONArray()
        val files = offer.getJSONArray("files")
        for (i in 0 until files.length()) {
            val f = files.getJSONObject(i); val n = f.getInt("n")
            if (alreadySaved(offer, f) != null) done.put(n) else have.put(n.toString(), JSONArray(chunksOf(d, n).sorted()))
        }
        return 200 to JSONObject().put("have", have).put("done", done)
    }

    private fun alreadySaved(offer: JSONObject, f: JSONObject): String? {
        val rel = safeRel(f.getString("rel_path"))
        val key = folder(offer) + rel.dropLast(1).joinToString("") { "$it/" }
        return Names.sameAlready(queue.receivedIn(key), rel.last(), f.getString("sha256"))
    }

    private fun offer(s: Session, req: Req): Pair<Int, JSONObject> {
        val o = JSONObject(String(req.body))
        if (o.optString("from") != s.peer) throw err(403, "offer from someone else")
        if (o.has("hold_for") && !o.isNull("hold_for")) throw err(403, "this device does not hold shares")
        val d = dir(o.getString("offer_id"))
        val chunk = o.optInt("chunk", SendConst.CHUNK).coerceIn(1024, SendConst.CHUNK)
        o.put("chunk", chunk)
        val files = o.getJSONArray("files")
        var have = 0L
        for (i in 0 until files.length()) { val f = files.getJSONObject(i); have += Chunks.bytesHave(f.getLong("size"), chunksOf(d, f.getInt("n")), chunk) }
        val need = maxOf(0L, o.getLong("total") - have)
        d.mkdirs()
        val free = StatFs(d.path).availableBytes
        if (free < need) throw err(507, "not enough free space", JSONObject().put("missing_bytes", need - free))
        File(d, "offer.json").writeText(o.toString())
        return reply(d to o)
    }

    private fun chunk(id: String, n: Int, req: Req): Pair<Int, JSONObject> {
        val (d, offer) = findOffer(id)
        val i = req.query.split('&').firstOrNull { it.startsWith("chunk=") }?.substring(6)?.toIntOrNull() ?: throw err(400, "no chunk")
        val spec = (0 until offer.getJSONArray("files").length()).map { offer.getJSONArray("files").getJSONObject(it) }.firstOrNull { it.getInt("n") == n }
        val c = offer.getInt("chunk")
        if (spec == null || i < 0 || i >= Chunks.count(spec.getLong("size"), c)) throw err(400, "no such file or chunk")
        val claimed = req.headers["x-chunk-sha256"] ?: ""
        if (sha256Hex(req.body) != claimed || req.body.size != Chunks.length(spec.getLong("size"), i, c)) throw err(422, "chunk is damaged, send it again")
        synchronized(locks.getOrPut("$id/$n") { Any() }) {
            RandomAccessFile(File(d, "$n.part"), "rw").use { it.seek(Chunks.offset(i, c)); it.write(req.body) }
            File(d, "$n.chunks").appendText("$i\n")
        }
        return 200 to JSONObject().put("ok", true)
    }

    private fun sha256File(f: File): String {
        val md = MessageDigest.getInstance("SHA-256"); val buf = ByteArray(1 shl 20)
        f.inputStream().use { while (true) { val r = it.read(buf); if (r < 0) break; md.update(buf, 0, r) } }
        return hex(md.digest())
    }

    private fun done(id: String): Pair<Int, JSONObject> {
        val (d, offer) = findOffer(id)
        val c = offer.getInt("chunk"); val files = offer.getJSONArray("files")
        val bad = JSONObject(); val todo = ArrayList<JSONObject>()
        for (k in 0 until files.length()) {
            val f = files.getJSONObject(k); val n = f.getInt("n"); val size = f.getLong("size")
            if (alreadySaved(offer, f) != null) continue
            val missing = (0 until Chunks.count(size, c)).filter { it !in chunksOf(d, n) }
            val part = File(d, "$n.part")
            if (size == 0L && !part.exists()) part.createNewFile()
            if (missing.isNotEmpty()) { bad.put(n.toString(), JSONArray(missing)); continue }
            if (part.length() != size || sha256File(part) != f.getString("sha256")) {
                bad.put(n.toString(), JSONArray((0 until Chunks.count(size, c)).toList())); File(d, "$n.chunks").delete(); continue
            }
            todo += f
        }
        if (bad.length() > 0) throw err(409, "some files did not verify", JSONObject().put("bad", bad))
        val saved = JSONArray()
        for (k in 0 until files.length()) {
            val f = files.getJSONObject(k)
            val existing = alreadySaved(offer, f)
            if (existing != null) { saved.put(queue.pathOf(folder(offer) + safeRel(f.getString("rel_path")).dropLast(1).joinToString("") { "$it/" }, existing) ?: existing); continue }
            saved.put(publish(offer, f, File(d, "${f.getInt("n")}.part")))
        }
        d.deleteRecursively()
        val from = offer.optString("origin").ifBlank { offer.getString("from") }
        runCatching {  // Helios in the app can open these later, Argus or not
            val rows = (0 until files.length()).map { k -> val f = files.getJSONObject(k)
                SendQueue.MineFile(k, f.getString("rel_path"), saved.optString(k), f.getLong("size")) }
            val first = rows.first().name.substringBefore('/')
            val label = if (rows.map { it.name.substringBefore('/') }.toSet().size == 1) first else "${rows.size} files"
            queue.remember("received", offer.getString("offer_id"), label, book.nameOf(from), rows)
        }
        SendNotes.received(ctx, files.length(), book.nameOf(from), book.nameOf(from))
        queue.event("rx-" + offer.getString("offer_id"), JSONObject().put("kind", "received").put("device", self).put("peer", book.nameOf(from))
            .put("bytes", offer.getLong("total")).put("offer_id", offer.getString("offer_id")))
        return 200 to JSONObject().put("saved", saved)
    }

    // ---------------------------------------------------------------- publish: .part -> Download/Argus/From <sender>/
    private fun publish(offer: JSONObject, f: JSONObject, part: File): String {
        val rel = safeRel(f.getString("rel_path"))
        val relDir = folder(offer) + rel.dropLast(1).joinToString("") { "$it/" }
        val known = queue.receivedIn(relDir)
        val existing = known.keys.toMutableSet().also { it += listing(relDir) }
        val name = Names.pick(existing, rel.last())
        val mtime = f.optLong("mtime", 0).let { if (it > 0) it * 1000 else System.currentTimeMillis() }
        val mime = MimeTypeMap.getSingleton().getMimeTypeFromExtension(name.substringAfterLast('.', "").lowercase()) ?: "application/octet-stream"
        val path: String
        if (Build.VERSION.SDK_INT >= 29) {
            val r = ctx.contentResolver
            val v = ContentValues().apply { put(MediaStore.MediaColumns.DISPLAY_NAME, name); put(MediaStore.MediaColumns.MIME_TYPE, mime)
                put(MediaStore.MediaColumns.RELATIVE_PATH, relDir); put(MediaStore.MediaColumns.IS_PENDING, 1) }  // hidden until complete
            val uri = r.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, v) ?: throw err(500, "can't create the file")
            try {
                r.openOutputStream(uri)!!.use { o -> part.inputStream().use { it.copyTo(o, 1 shl 20) } }
                r.update(uri, ContentValues().apply { put(MediaStore.MediaColumns.IS_PENDING, 0); put(MediaStore.MediaColumns.DATE_MODIFIED, mtime / 1000) }, null, null)
            } catch (e: Exception) { r.delete(uri, null, null); throw e }
            path = relDir + name
        } else {
            val target = File(Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS), relDir.removePrefix("Download/") + name)
            target.parentFile?.mkdirs()
            part.copyTo(target, overwrite = false); target.setLastModified(mtime)
            MediaScannerConnection.scanFile(ctx, arrayOf(target.path), arrayOf(mime), null)  // gallery-visible
            path = target.path
        }
        queue.rememberReceived(relDir, name, f.getString("sha256"), path)
        return path
    }

    /** Names already in the folder (from MediaStore for what this app saved, or the directory on old Android). */
    private fun listing(relDir: String): Set<String> {
        val out = HashSet<String>()
        if (Build.VERSION.SDK_INT >= 29) {
            ctx.contentResolver.query(MediaStore.Downloads.EXTERNAL_CONTENT_URI, arrayOf(MediaStore.MediaColumns.DISPLAY_NAME),
                "${MediaStore.MediaColumns.RELATIVE_PATH}=?", arrayOf(relDir), null)?.use { while (it.moveToNext()) out += it.getString(0) }
        } else {
            File(Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS), relDir.removePrefix("Download/")).list()?.let { out += it }
        }
        return out
    }
}
