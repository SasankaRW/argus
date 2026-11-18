package dev.argus.companion.send

import android.content.ContentValues
import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/** This device's Ed25519 key: the 32-byte seed lives in the app's files dir, encrypted with an Android Keystore key. */
class DeviceKeys(ctx: Context) {
    val seed: ByteArray
    val pub: ByteArray

    init {
        val f = File(ctx.filesDir, "send-key.bin")
        seed = if (f.exists()) open(f.readBytes()) else SendCrypto.random(32).also { f.writeBytes(seal(it)) }
        pub = SendCrypto.edPublic(seed)
    }

    val pubB64: String get() = b64(pub)
    fun sign(data: ByteArray) = SendCrypto.sign(seed, data)

    private fun key(): SecretKey {
        val ks = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (ks.getKey("argus-send", null) as? SecretKey)?.let { return it }
        val g = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore")
        g.init(KeyGenParameterSpec.Builder("argus-send", KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
            .setBlockModes(KeyProperties.BLOCK_MODE_GCM).setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE).build())
        return g.generateKey()
    }

    private fun seal(d: ByteArray): ByteArray {
        val c = Cipher.getInstance("AES/GCM/NoPadding").apply { init(Cipher.ENCRYPT_MODE, key()) }
        return c.iv + c.doFinal(d)
    }

    private fun open(d: ByteArray): ByteArray {
        val c = Cipher.getInstance("AES/GCM/NoPadding").apply { init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, d, 0, 12)) }
        return c.doFinal(d, 12, d.size - 12)
    }
}

class Peer(val id: String, val name: String, val pub: ByteArray, val lan: List<String>, val tailscale: String?)

/** devices.json: the paired devices; works when Argus is down. A changed key for a known id is refused. */
class DeviceBook(private val ctx: Context) {
    private val file = File(ctx.filesDir, "devices.json")
    @Volatile private var peers: Map<String, Peer> = load()
    @Volatile var discovered: Map<String, String> = emptyMap()  // id -> ip:port, from NSD

    private fun load(): Map<String, Peer> = try {
        val a = JSONArray(file.readText())
        (0 until a.length()).map { parse(a.getJSONObject(it)) }.associateBy { it.id }
    } catch (e: Exception) { emptyMap() }

    private fun parse(o: JSONObject): Peer {
        val lan = o.optJSONArray("lan") ?: JSONArray()
        return Peer(o.getString("id"), o.optString("name", o.getString("id")), unb64(o.getString("pubkey")),
            (0 until lan.length()).map { lan.getString(it) }, o.optString("tailscale", "").ifBlank { null })
    }

    fun get(id: String) = peers[id]
    fun all() = peers.values.toList()
    fun nameOf(id: String) = peers[id]?.name ?: id

    @Synchronized fun merge(list: JSONArray, self: String): Boolean {
        var ok = true
        val next = peers.toMutableMap()
        for (i in 0 until list.length()) {
            val o = list.getJSONObject(i)
            val id = o.optString("id")
            if (id == self || id.isEmpty() || o.optString("pubkey").isEmpty()) continue
            val p = try { parse(o) } catch (e: Exception) { continue }
            val old = next[id]
            if (old != null && !old.pub.contentEquals(p.pub)) { ok = false; continue }
            next[id] = p
        }
        val changed = next.keys != peers.keys || next.any { (k, v) ->
            val o = peers[k]; o == null || o.lan != v.lan || o.tailscale != v.tailscale || o.name != v.name
        }
        if (changed) {
            peers = next
            val a = JSONArray()
            next.values.forEach { a.put(JSONObject().put("id", it.id).put("name", it.name).put("pubkey", b64(it.pub))
                .put("lan", JSONArray(it.lan)).put("tailscale", it.tailscale ?: "")) }
            val tmp = File(ctx.filesDir, "devices.json.tmp"); tmp.writeText(a.toString(1)); tmp.renameTo(file)
        }
        return ok
    }
}

class FileRow(val n: Int, val uri: String, val rel: String, val size: Long, val mtime: Long, var sha: String)

class ShareRow(val id: String, val target: String, val state: SendState, val reason: String, val created: Long,
               val total: Long, val sendAnyway: Boolean, val offerId: String, val files: List<FileRow>)

/** The sender's disk queue (SQLite). A share is written here before any byte is sent. */
class SendQueue private constructor(ctx: Context) : SQLiteOpenHelper(ctx, "send-queue.db", null, 2) {
    companion object {
        @Volatile private var inst: SendQueue? = null
        fun get(ctx: Context): SendQueue = inst ?: synchronized(this) { inst ?: SendQueue(ctx.applicationContext).also { inst = it } }
    }

    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL("CREATE TABLE shares(id TEXT PRIMARY KEY, target TEXT, state TEXT, reason TEXT DEFAULT '', created INTEGER, total INTEGER, send_anyway INTEGER DEFAULT 0, offer_id TEXT, asked INTEGER DEFAULT 0)")
        db.execSQL("CREATE TABLE files(share_id TEXT, n INTEGER, uri TEXT, rel TEXT, size INTEGER, mtime INTEGER, sha TEXT DEFAULT '', PRIMARY KEY(share_id, n))")
        db.execSQL("CREATE TABLE events(eid TEXT PRIMARY KEY, at INTEGER, json TEXT, sent INTEGER DEFAULT 0)")  // history + outbox
        db.execSQL("CREATE TABLE received(dir TEXT, name TEXT, sha TEXT, path TEXT, PRIMARY KEY(dir, name))")
        mineTables(db)
    }

    // what this phone sent and received, with where the files are: Helios in the app opens them, even offline
    private fun mineTables(db: SQLiteDatabase) {
        db.execSQL("CREATE TABLE IF NOT EXISTS mine(kind TEXT, offer_id TEXT, at INTEGER, label TEXT, peer TEXT, PRIMARY KEY(kind, offer_id))")
        db.execSQL("CREATE TABLE IF NOT EXISTS mine_files(kind TEXT, offer_id TEXT, n INTEGER, name TEXT, uri TEXT, size INTEGER, PRIMARY KEY(kind, offer_id, n))")
    }

    override fun onUpgrade(db: SQLiteDatabase, o: Int, n: Int) { if (o < 2) mineTables(db) }

    class MineFile(val n: Int, val name: String, val uri: String, val size: Long)
    class MineRow(val kind: String, val offerId: String, val at: Long, val label: String, val peer: String, val files: List<MineFile>)

    @Synchronized fun remember(kind: String, offerId: String, label: String, peer: String, files: List<MineFile>) {
        val db = writableDatabase
        db.beginTransaction()
        try {
            db.execSQL("INSERT OR REPLACE INTO mine(kind,offer_id,at,label,peer) VALUES(?,?,?,?,?)", arrayOf<Any>(kind, offerId, System.currentTimeMillis(), label, peer))
            db.execSQL("DELETE FROM mine_files WHERE kind=? AND offer_id=?", arrayOf<Any>(kind, offerId))
            for (f in files) db.execSQL("INSERT INTO mine_files(kind,offer_id,n,name,uri,size) VALUES(?,?,?,?,?,?)", arrayOf<Any>(kind, offerId, f.n, f.name, f.uri, f.size))
            // keep the newest 300
            db.execSQL("DELETE FROM mine WHERE (kind || offer_id) NOT IN (SELECT kind || offer_id FROM mine ORDER BY at DESC LIMIT 300)")
            db.execSQL("DELETE FROM mine_files WHERE (kind || offer_id) NOT IN (SELECT kind || offer_id FROM mine)")
            db.setTransactionSuccessful()
        } finally { db.endTransaction() }
    }

    @Synchronized fun mine(): List<MineRow> {
        val out = ArrayList<MineRow>()
        readableDatabase.rawQuery("SELECT kind,offer_id,at,label,peer FROM mine ORDER BY at DESC", null).use { c ->
            while (c.moveToNext()) out += MineRow(c.getString(0), c.getString(1), c.getLong(2), c.getString(3) ?: "", c.getString(4) ?: "", mineFiles(c.getString(0), c.getString(1)))
        }
        return out
    }

    @Synchronized fun mineFiles(kind: String, offerId: String): List<MineFile> {
        val out = ArrayList<MineFile>()
        readableDatabase.rawQuery("SELECT n,name,uri,size FROM mine_files WHERE kind=? AND offer_id=? ORDER BY n", arrayOf(kind, offerId)).use { c ->
            while (c.moveToNext()) out += MineFile(c.getInt(0), c.getString(1), c.getString(2), c.getLong(3))
        }
        return out
    }

    @Synchronized fun add(id: String, target: String, offerId: String, files: List<FileRow>) {
        val db = writableDatabase
        db.beginTransaction()
        try {
            db.insert("shares", null, ContentValues().apply { put("id", id); put("target", target); put("state", SendState.QUEUED.name)
                put("created", System.currentTimeMillis()); put("total", files.sumOf { it.size }); put("offer_id", offerId) })
            for (f in files) db.insert("files", null, ContentValues().apply { put("share_id", id); put("n", f.n); put("uri", f.uri)
                put("rel", f.rel); put("size", f.size); put("mtime", f.mtime); put("sha", f.sha) })
            db.setTransactionSuccessful()
        } finally { db.endTransaction() }
    }

    @Synchronized fun shares(vararg states: SendState): List<ShareRow> {
        val q = if (states.isEmpty()) "" else "WHERE state IN (" + states.joinToString(",") { "'" + it.name + "'" } + ")"
        val out = ArrayList<ShareRow>()
        readableDatabase.rawQuery("SELECT id,target,state,reason,created,total,send_anyway,offer_id FROM shares $q ORDER BY created", null).use { c ->
            while (c.moveToNext()) out += ShareRow(c.getString(0), c.getString(1), SendState.valueOf(c.getString(2)), c.getString(3),
                c.getLong(4), c.getLong(5), c.getInt(6) == 1, c.getString(7), files(c.getString(0)))
        }
        return out
    }

    fun share(id: String) = shares().firstOrNull { it.id == id }

    private fun files(id: String): List<FileRow> {
        val out = ArrayList<FileRow>()
        readableDatabase.rawQuery("SELECT n,uri,rel,size,mtime,sha FROM files WHERE share_id=? ORDER BY n", arrayOf(id)).use { c ->
            while (c.moveToNext()) out += FileRow(c.getInt(0), c.getString(1), c.getString(2), c.getLong(3), c.getLong(4), c.getString(5))
        }
        return out
    }

    @Synchronized fun setSha(id: String, n: Int, sha: String) {
        writableDatabase.execSQL("UPDATE files SET sha=? WHERE share_id=? AND n=?", arrayOf<Any>(sha, id, n))
    }

    /** Moves a share to a new state when the transition is allowed (see Transitions). Returns false when refused. */
    @Synchronized fun move(id: String, to: SendState, reason: String = ""): Boolean {
        val cur = share(id)?.state ?: return false
        if (!Transitions.allowed(cur, to)) return false
        writableDatabase.execSQL("UPDATE shares SET state=?, reason=? WHERE id=?", arrayOf<Any>(to.name, reason, id))
        return true
    }

    @Synchronized fun sendAnyway(id: String) { writableDatabase.execSQL("UPDATE shares SET send_anyway=1 WHERE id=?", arrayOf<Any>(id)) }
    @Synchronized fun keepWaiting(id: String) { writableDatabase.execSQL("UPDATE shares SET created=?, asked=0 WHERE id=?", arrayOf<Any>(System.currentTimeMillis(), id)) }

    /** True the first time it is called for a share (so the "keep waiting?" question is asked once). */
    @Synchronized fun markAsked(id: String): Boolean {
        val already = readableDatabase.rawQuery("SELECT asked FROM shares WHERE id=?", arrayOf(id)).use { it.moveToFirst() && it.getInt(0) == 1 }
        if (!already) writableDatabase.execSQL("UPDATE shares SET asked=1 WHERE id=?", arrayOf<Any>(id))
        return !already
    }

    /** After a crash: a share that was mid-send goes back to the queue. */
    @Synchronized fun recover() { writableDatabase.execSQL("UPDATE shares SET state='QUEUED' WHERE state IN ('SENDING','VERIFYING')") }

    /** Drops a finished share's rows (only after the receiver's 200 on /done). */
    @Synchronized fun remove(id: String) {
        writableDatabase.execSQL("DELETE FROM files WHERE share_id=?", arrayOf<Any>(id)); writableDatabase.execSQL("DELETE FROM shares WHERE id=?", arrayOf<Any>(id))
    }

    @Synchronized fun event(eid: String, json: JSONObject) {
        val db = writableDatabase
        db.execSQL("INSERT OR IGNORE INTO events(eid,at,json) VALUES(?,?,?)", arrayOf<Any>(eid, System.currentTimeMillis(), json.toString()))
        db.execSQL("DELETE FROM events WHERE sent=1 AND eid NOT IN (SELECT eid FROM events ORDER BY at DESC LIMIT 200)")  // keeps the last 200
    }

    @Synchronized fun unsent(): List<Pair<String, String>> {
        val out = ArrayList<Pair<String, String>>()
        readableDatabase.rawQuery("SELECT eid,json FROM events WHERE sent=0 ORDER BY at LIMIT 50", null).use { while (it.moveToNext()) out += it.getString(0) to it.getString(1) }
        return out
    }

    @Synchronized fun sentEvent(eid: String) { writableDatabase.execSQL("UPDATE events SET sent=1 WHERE eid=?", arrayOf<Any>(eid)) }

    // what the receiver has saved, for the name rule and the same-sha skip
    @Synchronized fun receivedIn(dir: String): Map<String, String> {
        val m = HashMap<String, String>()
        readableDatabase.rawQuery("SELECT name,sha FROM received WHERE dir=?", arrayOf(dir)).use { while (it.moveToNext()) m[it.getString(0)] = it.getString(1) }
        return m
    }

    @Synchronized fun rememberReceived(dir: String, name: String, sha: String, path: String) {
        writableDatabase.execSQL("INSERT OR REPLACE INTO received(dir,name,sha,path) VALUES(?,?,?,?)", arrayOf<Any>(dir, name, sha, path))
    }

    @Synchronized fun pathOf(dir: String, name: String): String? =
        readableDatabase.rawQuery("SELECT path FROM received WHERE dir=? AND name=?", arrayOf(dir, name)).use { if (it.moveToFirst()) it.getString(0) else null }
}
