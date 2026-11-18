package dev.argus.companion.send

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ClipCoreTest {
    private val sessionKey = ByteArray(32) { it.toByte() }
    private val nonce = ByteArray(12) { it.toByte() }
    private val plain = """{"text":"hello"}""".toByteArray()

    @Test fun sharedVectorKey() {
        assertEquals("b52c0827574da950beaf28d43afd56c5d15acd46885808c023a619251c9bf3a4", hex(ClipCrypto.clipKey(sessionKey)))
    }

    @Test fun sharedVectorCiphertext() {
        val ct = ClipCrypto.encrypt(ClipCrypto.clipKey(sessionKey), nonce, plain)
        assertEquals("OOYJvIsdC7IC6jK/7SwwxP9ypPKsANs3XdeBa0E92G4=", b64(ct))
        assertArrayEquals(plain, ClipCrypto.decrypt(ClipCrypto.clipKey(sessionKey), nonce, ct))
    }

    @Test fun sealOpenRoundTripAndTamper() {
        val body = ClipCrypto.seal(sessionKey, plain)
        assertArrayEquals(plain, ClipCrypto.open(sessionKey, body))
        val other = ByteArray(32) { 9 }
        try { ClipCrypto.open(other, body); org.junit.Assert.fail("wrong key must not open") } catch (e: java.security.GeneralSecurityException) {}
    }

    @Test fun dedupeAndOrdering() {
        val l = ClipLedger()
        assertTrue(l.accept("a", "pc", 1_000_000))
        assertFalse(l.accept("a", "pc", 1_000_001))                       // same id
        assertTrue(l.accept("b", "pc", 1_000_000 - 4 * 60_000))           // slightly older: inside the 5 min tolerance
        assertFalse(l.accept("c", "pc", 1_000_000 - 6 * 60_000))          // too old
        assertTrue(l.accept("d", "phone", 5))                             // other sender: clocks never meet
        for (i in 0 until 70) l.accept("x$i", "pc", 2_000_000L + i)
        assertTrue(l.accept("a", "pc", 3_000_000))                        // "a" fell out of the last 64
    }

    @Test fun echoGuard() {
        val l = ClipLedger()
        assertFalse(l.isEcho("hi"))
        l.wrote("hi"); assertTrue(l.isEcho("hi")); assertFalse(l.isEcho("ho"))
    }

    @Test fun linkDetection() {
        assertTrue(ClipRules.isLink("https://example.com/a?b=1"))
        assertTrue(ClipRules.isLink("  http://example.com \n"))
        assertFalse(ClipRules.isLink("see https://example.com"))
        assertFalse(ClipRules.isLink("ftp://example.com"))
        assertFalse(ClipRules.isLink("javascript:alert(1)"))
        assertFalse(ClipRules.isLink("https://"))
        assertEquals("example.com/a", ClipRules.shortLink("https://example.com/a?x=1"))
        assertEquals(60, ClipRules.shortLink("https://example.com/" + "a".repeat(100)).length)
    }
}
