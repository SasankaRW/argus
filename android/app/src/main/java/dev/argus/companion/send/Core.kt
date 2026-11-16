package dev.argus.companion.send

import org.bouncycastle.crypto.agreement.X25519Agreement
import org.bouncycastle.crypto.digests.SHA256Digest
import org.bouncycastle.crypto.generators.HKDFBytesGenerator
import org.bouncycastle.crypto.params.Ed25519PrivateKeyParameters
import org.bouncycastle.crypto.params.Ed25519PublicKeyParameters
import org.bouncycastle.crypto.params.HKDFParameters
import org.bouncycastle.crypto.params.X25519PrivateKeyParameters
import org.bouncycastle.crypto.params.X25519PublicKeyParameters
import org.bouncycastle.crypto.signers.Ed25519Signer
import java.security.MessageDigest
import java.security.SecureRandom
import java.util.Base64
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec

/** Constants of the Argus Send contract (send-protocol.md). */
object SendConst {
    const val PORT = 8610
    const val CHUNK = 8 * 1024 * 1024
    const val SMALL = 1024 * 1024
    const val PARALLEL = 4
    const val SESSION_MS = 10 * 60_000L
    const val MOBILE_LIMIT = 200L * 1024 * 1024
    const val SERVICE_TYPE = "_argus-send._tcp"
    val INFO = "argus-send v1".toByteArray()
}

fun b64(d: ByteArray): String = Base64.getEncoder().encodeToString(d)
fun unb64(s: String): ByteArray = Base64.getDecoder().decode(s)
fun hex(d: ByteArray): String = d.joinToString("") { "%02x".format(it) }
fun sha256Hex(d: ByteArray): String = hex(MessageDigest.getInstance("SHA-256").digest(d))

/** Crypto of the Auth section: Ed25519 identity, X25519 ephemeral, HKDF session key, per-request HMAC. */
object SendCrypto {
    private val rnd = SecureRandom()

    fun random(n: Int) = ByteArray(n).also { rnd.nextBytes(it) }

    fun edPublic(seed: ByteArray): ByteArray = Ed25519PrivateKeyParameters(seed, 0).generatePublicKey().encoded

    fun sign(seed: ByteArray, data: ByteArray): ByteArray = Ed25519Signer().run {
        init(true, Ed25519PrivateKeyParameters(seed, 0)); update(data, 0, data.size); generateSignature()
    }

    fun verify(pub: ByteArray, data: ByteArray, sig: ByteArray): Boolean = try {
        Ed25519Signer().run {
            init(false, Ed25519PublicKeyParameters(pub, 0)); update(data, 0, data.size); verifySignature(sig)
        }
    } catch (e: Exception) { false }

    fun xPublic(priv: ByteArray): ByteArray = X25519PrivateKeyParameters(priv, 0).generatePublicKey().encoded

    /** K = HKDF-SHA256(X25519(eph), salt = nonce_c||nonce_s, info = "argus-send v1" || both pubkeys sorted bytewise). */
    fun sessionKey(myEphPriv: ByteArray, peerEphPub: ByteArray, nonceC: ByteArray, nonceS: ByteArray,
                   pubA: ByteArray, pubB: ByteArray): ByteArray {
        val secret = ByteArray(32)
        X25519Agreement().run { init(X25519PrivateKeyParameters(myEphPriv, 0)); calculateAgreement(X25519PublicKeyParameters(peerEphPub, 0), secret, 0) }
        val (lo, hi) = if (compare(pubA, pubB) <= 0) pubA to pubB else pubB to pubA
        val g = HKDFBytesGenerator(SHA256Digest())
        g.init(HKDFParameters(secret, nonceC + nonceS, SendConst.INFO + lo + hi))
        return ByteArray(32).also { g.generateBytes(it, 0, 32) }
    }

    private fun compare(a: ByteArray, b: ByteArray): Int {
        for (i in 0 until minOf(a.size, b.size)) { val d = (a[i].toInt() and 255) - (b[i].toInt() and 255); if (d != 0) return d }
        return a.size - b.size
    }

    /** HMAC-SHA256(K, method|path|query|sha256(body)|seq), hex. The seq is inside the MAC (replay protection). */
    fun mac(key: ByteArray, method: String, path: String, query: String, bodySha: String, seq: Long): String {
        val m = Mac.getInstance("HmacSHA256"); m.init(SecretKeySpec(key, "HmacSHA256"))
        return hex(m.doFinal("${method.uppercase()}|$path|$query|$bodySha|$seq".toByteArray()))
    }

    fun macOk(key: ByteArray, method: String, path: String, query: String, bodySha: String, seq: Long, got: String) =
        MessageDigest.isEqual(mac(key, method, path, query, bodySha, seq).toByteArray(), got.toByteArray())
}

/** One receiver-side session: key, expiry and the seq numbers already seen (a repeat is refused). */
class SeqWindow {
    private val seen = HashSet<Long>()
    @Synchronized fun accept(seq: Long): Boolean = seq >= 1 && seen.add(seq)
}

/** Chunk arithmetic and the resume map. */
object Chunks {
    fun count(size: Long, chunk: Int = SendConst.CHUNK): Int = ((size + chunk - 1) / chunk).toInt()
    fun offset(i: Int, chunk: Int = SendConst.CHUNK): Long = i.toLong() * chunk
    fun length(size: Long, i: Int, chunk: Int = SendConst.CHUNK): Int = minOf(chunk.toLong(), size - offset(i, chunk)).toInt()

    /** Chunks still to send, given what the receiver already holds. */
    fun pending(size: Long, have: Collection<Int>, chunk: Int = SendConst.CHUNK): List<Int> {
        val h = have.toSet(); return (0 until count(size, chunk)).filter { it !in h }
    }

    fun bytesHave(size: Long, have: Collection<Int>, chunk: Int = SendConst.CHUNK): Long =
        have.toSet().filter { it in 0 until count(size, chunk) }.sumOf { length(size, it, chunk).toLong() }
}

/** The name rule: an existing name becomes "name (2).ext", "(3)"...; the same sha256 under any variant is not saved again. */
object Names {
    private fun split(name: String): Pair<String, String> {
        val dot = name.lastIndexOf('.')
        return if (dot <= 0) name to "" else name.substring(0, dot) to name.substring(dot)
    }

    fun variants(name: String): Sequence<String> {
        val (stem, ext) = split(name)
        return sequence { yield(name); var k = 2; while (true) { yield("$stem ($k)$ext"); k++ } }
    }

    /** First free name. */
    fun pick(existing: Set<String>, name: String): String = variants(name).first { it !in existing }

    /** An already-saved variant with this sha256: walk the variants until a free slot; the first match wins. */
    fun sameAlready(known: Map<String, String>, name: String, sha: String): String? {
        for (v in variants(name)) { val s = known[v] ?: return null; if (s == sha) return v }
        return null
    }
}

enum class SendState { QUEUED, SENDING, VERIFYING, DONE, HELD, FAILED, CANCELLED }

object Transitions {
    private val ok = mapOf(
        SendState.QUEUED to setOf(SendState.SENDING, SendState.HELD, SendState.FAILED, SendState.CANCELLED),
        SendState.SENDING to setOf(SendState.VERIFYING, SendState.QUEUED, SendState.HELD, SendState.FAILED, SendState.CANCELLED),
        SendState.VERIFYING to setOf(SendState.DONE, SendState.SENDING, SendState.QUEUED, SendState.FAILED, SendState.CANCELLED),
        SendState.HELD to setOf(SendState.SENDING, SendState.QUEUED, SendState.FAILED, SendState.CANCELLED),
        SendState.FAILED to setOf(SendState.QUEUED),  // "Try again"
        SendState.DONE to emptySet(),
        SendState.CANCELLED to emptySet(),
    )

    fun allowed(from: SendState, to: SendState) = from == to || to in ok.getValue(from)

    /** Mobile-data rule: over 200 MB waits for an unmetered network unless the user chose "Send anyway". */
    fun waitsForWifi(total: Long, metered: Boolean, sendAnyway: Boolean) = metered && total > SendConst.MOBILE_LIMIT && !sendAnyway
}
