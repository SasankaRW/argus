package dev.argus.companion

/**
 * Will Android really show a message? A notification posted to a channel you switched off is dropped without a word,
 * so the inbox would acknowledge a message nobody saw. This says why not (sent to Argus, shown on Helios's Phone box),
 * or null when it will show. Pure, so it is tested on the JVM.
 */
object ShowCheck {
    private const val IMPORTANCE_NONE = 0  // NotificationManager.IMPORTANCE_NONE: the channel is off

    fun blockReason(appEnabled: Boolean, channelName: String, channelImportance: Int?): String? = when {
        !appEnabled -> "notifications are off for Argus"
        channelImportance == IMPORTANCE_NONE -> "the \"$channelName\" notifications are off"
        else -> null
    }
}
