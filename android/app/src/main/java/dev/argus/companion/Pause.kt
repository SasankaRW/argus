package dev.argus.companion

import java.util.concurrent.Semaphore
import java.util.concurrent.TimeUnit

/** A sleep that can be cut short: the phone got a network again, so there is no point waiting out the back-off. */
class Pause {
    private val gate = Semaphore(0)

    @Throws(InterruptedException::class)
    fun sleep(ms: Long) {
        gate.tryAcquire(ms, TimeUnit.MILLISECONDS)
    }

    fun kick() {
        if (gate.availablePermits() == 0) gate.release()
    }
}
