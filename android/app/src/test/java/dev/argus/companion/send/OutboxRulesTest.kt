package dev.argus.companion.send

import dev.argus.companion.send.OutboxAction.DELETE
import dev.argus.companion.send.OutboxAction.KEEP
import dev.argus.companion.send.OutboxAction.REMOVE_AND_DELETE
import dev.argus.companion.send.OutboxRules.Folder
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class OutboxRulesTest {
    private val now = 1_800_000_000_000L
    private val day = 24L * 60 * 60 * 1000
    private val mib = 1024 * 1024L

    @Test fun protectedStatesAreKept() {
        for (s in listOf(SendState.QUEUED, SendState.SENDING, SendState.VERIFYING, SendState.HELD, SendState.DONE))
            assertEquals(KEEP, OutboxRules.decide(s, now - 90 * day, now))
    }

    @Test fun orphansAndCancelledGoAfter30Days() {
        assertEquals(DELETE, OutboxRules.decide(null, now - 31 * day, now))
        assertEquals(DELETE, OutboxRules.decide(SendState.CANCELLED, now - 31 * day, now))
        assertEquals(KEEP, OutboxRules.decide(null, now - 29 * day, now))
        assertEquals(KEEP, OutboxRules.decide(SendState.CANCELLED, now - 29 * day, now))
    }

    @Test fun failedIsRemovedAfter30Days() {
        assertEquals(REMOVE_AND_DELETE, OutboxRules.decide(SendState.FAILED, now - 31 * day, now))
        assertEquals(KEEP, OutboxRules.decide(SendState.FAILED, now - 2 * day, now))
    }

    @Test fun graceAndFutureAreKept() {
        assertEquals(KEEP, OutboxRules.decide(null, now - 1000, now))
        assertEquals(KEEP, OutboxRules.decide(null, now + 40 * day, now))
        assertEquals(KEEP, OutboxRules.decide(SendState.FAILED, now + 40 * day, now))
        assertFalse(OutboxRules.spaceDeletable(null, now - 60_000, now))
        assertFalse(OutboxRules.spaceDeletable(null, now + day, now))
    }

    @Test fun capDeletesOldestDeletableUntilUnderTarget() {
        val f = listOf(
            Folder("held", now - 50 * day, 200 * mib, SendState.HELD),   // protected, even though oldest
            Folder("a", now - 20 * day, 150 * mib, null),
            Folder("b", now - 10 * day, 100 * mib, SendState.CANCELLED),
            Folder("c", now - 5 * day, 100 * mib, null),
            Folder("fresh", now - 1000, 100 * mib, null))                // inside the grace
        // 650 MB: a -> 500, b -> 400 (not under 400 yet), c -> 300
        assertEquals(listOf("a", "b", "c"), OutboxRules.overCap(f, now))
    }

    @Test fun underCapDeletesNothing() {
        assertTrue(OutboxRules.overCap(listOf(Folder("a", now - 40 * day, 499 * mib, null)), now).isEmpty())
    }
}
