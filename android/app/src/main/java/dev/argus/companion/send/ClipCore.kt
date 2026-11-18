package dev.argus.companion.send

import org.json.JSONObject
import java.security.SecureRandom
import javax.crypto.Cipher
import javax.crypto.Mac
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec

/** Clipboard sync wire format (docs/superpowers/specs/2026-11-18-clipboard-sync-design.md): no Android classes, so JVM tests cover it. */
object ClipCrypto {
    private val INFO = "argus clip v1".toByteArray()
    private val rnd = SecureRandom()

    /** HKDF-SHA256 (RFC 5869), salt none = 32 zero bytes, 32 bytes out: one block, T(1) = HMAC(PRK, info || 0x01). */
    fun clipKey(sessionKey: ByteArray): ByteArray {
        val prk = hmac(ByteArray(32), sessionKey)
        return hmac(prk, INFO + byteArrayOf(1))
    }

    private fun hmac(key: ByteArray, data: ByteArray): ByteArray =
        Mac.getInstance("HmacSHA256").run { init(SecretKeySpec(key, "HmacSHA256")); doFinal(data) }

    /** AES-256-GCM, 128-bit tag, no AAD; returns ciphertext||tag. */
    fun encrypt(clipKey: ByteArray, nonce: ByteArray, plain: ByteArray): ByteArray =
        Cipher.getInstance("AES/GCM/NoPadding").run {
            init(Cipher.ENCRYPT_MODE, SecretKeySpec(clipKey, "AES"), GCMParameterSpec(128, nonce)); doFinal(plain)
        }

    fun decrypt(clipKey: ByteArray, nonce: ByteArray, sealed: ByteArray): ByteArray =
        Cipher.getInstance("AES/GCM/NoPadding").run {
            init(Cipher.DECRYPT_MODE, SecretKeySpec(clipKey, "AES"), GCMParameterSpec(128, nonce)); doFinal(sealed)
        }

    /** The request body: {"n": b64 nonce, "c": b64 ciphertext+tag}. */
    fun seal(sessionKey: ByteArray, plain: ByteArray): String {
        val nonce = ByteArray(12).also { rnd.nextBytes(it) }
        return JSONObject().put("n", b64(nonce)).put("c", b64(encrypt(clipKey(sessionKey), nonce, plain))).toString()
    }

    fun open(sessionKey: ByteArray, body: String): ByteArray {
        val j = JSONObject(body)
        val nonce = unb64(j.getString("n"))
        if (nonce.size != 12) throw IllegalArgumentException("bad nonce")
        return decrypt(clipKey(sessionKey), nonce, unb64(j.getString("c")))
    }
}

/** What the receiver remembers, in RAM only: the last 64 ids, the newest `at` per sender, the hash of the last text written here. */
class ClipLedger {
    companion object { const val IDS = 64; const val TOLERANCE_MS = 5 * 60_000L }

    private val ids = object : LinkedHashMap<String, Boolean>() {
        override fun removeEldestEntry(eldest: MutableMap.MutableEntry<String, Boolean>?) = size > IDS
    }
    private val newest = HashMap<String, Long>()
    @Volatile private var lastWritten: String = ""

    /** True when the clip is fresh and recorded; false for a repeated id or one more than 5 minutes older than the newest from that sender. */
    @Synchronized fun accept(id: String, from: String, at: Long): Boolean {
        if (ids.containsKey(id)) return false
        val top = newest[from]
        if (top != null && at < top - TOLERANCE_MS) return false
        ids[id] = true
        if (top == null || at > top) newest[from] = at
        return true
    }

    fun wrote(text: String) { lastWritten = hash(text) }
    fun isEcho(text: String) = lastWritten.isNotEmpty() && lastWritten == hash(text)

    private fun hash(t: String) = sha256Hex(t.toByteArray())
}

object ClipRules {
    const val MAX_BYTES = 256 * 1024

    /** A single http(s) URL with a host and no whitespace. */
    fun isLink(text: String): Boolean {
        val t = text.trim()
        if (t.isEmpty() || t.any { it.isWhitespace() }) return false
        return try { val u = java.net.URI(t); (u.scheme == "http" || u.scheme == "https") && !u.host.isNullOrEmpty() } catch (e: Exception) { false }
    }

    /** Host and path for a notification, shortened to 60 characters. */
    fun shortLink(link: String): String {
        val t = try { java.net.URI(link.trim()).let { (it.host ?: "") + (it.rawPath ?: "") } } catch (e: Exception) { link }
        return if (t.length > 60) t.take(59) + "…" else t
    }

    fun sizeOk(text: String) = text.isNotEmpty() && text.toByteArray().size <= MAX_BYTES
}
