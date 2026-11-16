package dev.argus.companion.send

import org.json.JSONObject
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

class SendCoreTest {
    private val mib = 1024 * 1024L

    @Test fun chunkMath() {
        assertEquals(0, Chunks.count(0)); assertEquals(1, Chunks.count(1)); assertEquals(1, Chunks.count(8 * mib))
        assertEquals(2, Chunks.count(8 * mib + 1)); assertEquals(3, Chunks.count(20 * mib))
        assertEquals(8 * mib.toInt(), Chunks.length(20 * mib, 1)); assertEquals(4 * mib.toInt(), Chunks.length(20 * mib, 2))
        assertEquals(16 * mib, Chunks.offset(2))
    }

    @Test fun resumeMap() {
        assertEquals(listOf(1, 3), Chunks.pending(30 * mib, listOf(0, 2)))           // 4 chunks, 0 and 2 already held
        assertEquals(emptyList<Int>(), Chunks.pending(16 * mib, listOf(0, 1, 1, 9)))  // duplicates and strangers ignored
        assertEquals(8 * mib + 6 * mib, Chunks.bytesHave(30 * mib, listOf(0, 3)))     // chunk 3 is the 6 MiB tail
        assertEquals(listOf(0, 1), Chunks.pending(10 * mib, emptyList()))
    }

    private fun vec() = JSONObject(File("../../docs/send-test-vectors.json").readText())

    @Test fun sessionKeyAndMacMatchPython() {
        val v = vec()
        fun b(k: String) = unb64(v.getString(k))
        assertArrayEquals(b("pub_c"), SendCrypto.edPublic(b("ed_seed_c")))
        assertArrayEquals(b("eph_s"), SendCrypto.xPublic(b("x_priv_s")))
        val kc = SendCrypto.sessionKey(b("x_priv_c"), b("eph_s"), b("nonce_c"), b("nonce_s"), b("pub_c"), b("pub_s"))
        val ks = SendCrypto.sessionKey(b("x_priv_s"), b("eph_c"), b("nonce_c"), b("nonce_s"), b("pub_s"), b("pub_c"))
        assertArrayEquals(b("K"), kc); assertArrayEquals(kc, ks)
        // Ed25519 is deterministic: both signatures equal the Python ones, and verify
        assertArrayEquals(b("sig_s"), SendCrypto.sign(b("ed_seed_s"), b("nonce_c") + b("eph_s")))
        assertTrue(SendCrypto.verify(b("pub_c"), b("nonce_s") + b("eph_c"), b("proof_c")))
        assertFalse(SendCrypto.verify(b("pub_c"), b("nonce_s") + b("eph_s"), b("proof_c")))
        val m = v.getJSONObject("mac")
        assertEquals(m.getString("body_sha256"), sha256Hex(m.getString("body").toByteArray()))
        assertEquals(m.getString("mac"), SendCrypto.mac(kc, m.getString("method"), m.getString("path"), m.getString("query"), m.getString("body_sha256"), m.getLong("seq")))
    }

    @Test fun macAndSeqCheck() {
        val k = ByteArray(32) { it.toByte() }
        val mac = SendCrypto.mac(k, "put", "/send/offers/a/files/0", "chunk=1", "ab", 5)
        assertTrue(SendCrypto.macOk(k, "PUT", "/send/offers/a/files/0", "chunk=1", "ab", 5, mac))
        assertFalse(SendCrypto.macOk(k, "PUT", "/send/offers/a/files/0", "chunk=2", "ab", 5, mac))  // query is covered
        assertFalse(SendCrypto.macOk(k, "PUT", "/send/offers/a/files/0", "chunk=1", "ab", 6, mac))  // so is the seq
        assertFalse(SendCrypto.macOk(ByteArray(32), "PUT", "/send/offers/a/files/0", "chunk=1", "ab", 5, mac))
        val w = SeqWindow()
        assertFalse(w.accept(0)); assertTrue(w.accept(1)); assertTrue(w.accept(3)); assertFalse(w.accept(3)); assertTrue(w.accept(2))
    }

    @Test fun nameCollisionRule() {
        assertEquals("a.txt", Names.pick(emptySet(), "a.txt"))
        assertEquals("a (2).txt", Names.pick(setOf("a.txt"), "a.txt"))
        assertEquals("a (3).txt", Names.pick(setOf("a.txt", "a (2).txt"), "a.txt"))
        assertEquals("noext (2)", Names.pick(setOf("noext"), "noext"))
        assertEquals(".hidden (2)", Names.pick(setOf(".hidden"), ".hidden"))
        // same sha256 under any variant: not saved again; a different one gets the next free name
        val known = mapOf("a.txt" to "h1", "a (2).txt" to "h2")
        assertEquals("a (2).txt", Names.sameAlready(known, "a.txt", "h2"))
        assertEquals("a.txt", Names.sameAlready(known, "a.txt", "h1"))
        assertNull(Names.sameAlready(known, "a.txt", "h3"))
        assertEquals("a (3).txt", Names.pick(known.keys, "a.txt"))
    }

    @Test fun queueStateTransitions() {
        fun ok(a: SendState, b: SendState) = assertTrue("$a -> $b", Transitions.allowed(a, b))
        fun no(a: SendState, b: SendState) = assertFalse("$a -> $b", Transitions.allowed(a, b))
        ok(SendState.QUEUED, SendState.SENDING); ok(SendState.SENDING, SendState.VERIFYING); ok(SendState.VERIFYING, SendState.DONE)
        ok(SendState.SENDING, SendState.HELD); ok(SendState.HELD, SendState.SENDING)       // handed to Calypso, then sent directly
        ok(SendState.VERIFYING, SendState.SENDING)                                         // 409: bad chunks sent again
        ok(SendState.FAILED, SendState.QUEUED)                                             // "Try again"
        no(SendState.QUEUED, SendState.DONE); no(SendState.QUEUED, SendState.VERIFYING)    // nothing is done before it is verified
        no(SendState.DONE, SendState.QUEUED); no(SendState.CANCELLED, SendState.QUEUED); no(SendState.FAILED, SendState.DONE)
        assertTrue(Transitions.waitsForWifi(201 * mib, true, false))
        assertFalse(Transitions.waitsForWifi(200 * mib, true, false)); assertFalse(Transitions.waitsForWifi(500 * mib, false, false))
        assertFalse(Transitions.waitsForWifi(500 * mib, true, true))                       // "Send anyway"
    }
}
