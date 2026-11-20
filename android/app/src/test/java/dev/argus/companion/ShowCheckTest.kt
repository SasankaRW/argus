package dev.argus.companion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class ShowCheckTest {
    @Test fun fineWhenAndroidWillShowIt() {
        assertNull(ShowCheck.blockReason(true, "Approvals and warnings", 4))
    }

    @Test fun allNotificationsOff() {
        assertEquals("notifications are off for Argus", ShowCheck.blockReason(false, "Approvals and warnings", 4))
    }

    @Test fun oneChannelOff() {
        assertEquals("the \"Approvals and warnings\" notifications are off",
            ShowCheck.blockReason(true, "Approvals and warnings", 0))
    }

    @Test fun aChannelNotMadeYetIsFine() {
        assertNull(ShowCheck.blockReason(true, "Messages", null))
    }

    @Test fun aChannelSwitchedBackOnLeavesTheOtherBlocked() {
        val off = mapOf(4 to "the \"Approvals and warnings\" notifications are off")
        val (left, why) = ShowCheck.recheck(setOf(3, 4)) { off[it] }
        assertEquals(setOf(4), left)
        assertEquals("the \"Approvals and warnings\" notifications are off", why)
    }

    @Test fun allBackOnClearsIt() {
        val (left, why) = ShowCheck.recheck(setOf(3, 4)) { null }
        assertEquals(emptySet<Int>(), left)
        assertNull(why)
    }
}
