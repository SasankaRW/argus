package dev.argus.companion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class LiveRankTest {
    private val now = 1_800_000_000L
    private fun a(id: String, kind: String, started: Double = 1.0, ends: Long? = null, device: String? = null) =
        Activity(id, kind, id, "", "", null, ends, started, device)

    @Test fun orderIsUrgentTimerTransferTimerJobCustom() {
        val items = listOf(a("c", "custom"), a("j", "job"), a("t", "timer", ends = now + 600), a("x", "transfer", device = "pc"),
            a("u", "timer", ends = now + 30))
        assertEquals("u", LiveRank.pick(items, now)?.id)
        assertEquals("x", LiveRank.pick(items.filter { it.id != "u" }, now)?.id)
        assertEquals("t", LiveRank.pick(items.filter { it.id !in listOf("u", "x") }, now)?.id)
        assertEquals("j", LiveRank.pick(items.filter { it.id in listOf("j", "c") }, now)?.id)
    }

    @Test fun tiesGoToNewest() {
        assertEquals("new", LiveRank.pick(listOf(a("old", "job", 1.0), a("new", "job", 2.0)), now)?.id)
    }

    @Test fun skipsPhoneTransfersAndPhoneTimers() {
        val items = listOf(a("s", "transfer", device = "phone"), a("phone.timer.1", "timer", ends = now + 20))
        assertNull(LiveRank.pick(items, now))
        assertEquals("s2", LiveRank.pick(items + a("s2", "transfer", device = null), now)?.id)
    }

    @Test fun reRankDelayIsWhenATimerTurnsUrgent() {
        assertEquals(40L, LiveRank.secsUntilReRank(listOf(a("t", "timer", ends = now + 100)), now))
        assertNull(LiveRank.secsUntilReRank(listOf(a("t", "timer", ends = now + 30)), now))
    }

    @Test fun routes() {
        assertEquals("/helios/?app=1#transfers", LiveRank.route(a("x", "transfer")))
        assertEquals("/helios/?app=1#map", LiveRank.route(a("j", "job")))
    }

    @Test fun parsesNulls() {
        val o = org.json.JSONObject("""{"id":"j","kind":"job","title":"T","detail":"d","icon":"x","progress":null,"ends_at":null,"started_at":5.5,"device":null,"priority":1}""")
        val p = Activity.parse(o)
        assertNull(p.progress); assertNull(p.endsAt); assertNull(p.device); assertEquals(5.5, p.startedAt, 0.0)
    }
}
