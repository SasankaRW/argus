package dev.argus.companion

import android.app.Notification
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraManager
import android.media.AudioAttributes
import android.media.AudioManager
import android.media.MediaPlayer
import android.media.RingtoneManager
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.net.Uri
import android.os.BatteryManager
import android.os.Environment
import android.os.Handler
import android.os.Looper
import android.os.StatFs
import android.provider.AlarmClock
import android.provider.Settings
import android.view.KeyEvent
import org.json.JSONObject

/** A job Argus can't do; its message is shown to you as is. */
class ActionError(message: String) : Exception(message)

/**
 * The phone plugin's workflows (plugins/phone/plugin.yaml). Dry-run (the plugin isn't in plugins.live): says what
 * it would do and does nothing.
 */
class Actions(private val ctx: Context) {

    fun run(workflow: String, input: JSONObject, live: Boolean): JSONObject = when (workflow) {
        "status" -> status()
        "ring" -> act(live, "ring the phone for 20 seconds") { ring() }
        "dnd" -> {
            val on = onOff(input)
            act(live, "turn Do Not Disturb ${if (on) "on" else "off"}") { dnd(on) }
        }
        "torch" -> {
            val on = onOff(input)
            act(live, "turn the torch ${if (on) "on" else "off"}") { torch(on) }
        }
        "timer" -> {
            val minutes = input.optInt("minutes", 0)
            if (minutes !in 1..1440) throw ActionError("a timer is 1 to 1440 minutes")
            val label = input.optString("label", "").take(60)
            act(live, "start a $minutes minute timer") { timer(minutes, label) }
        }
        "open_app" -> {
            val name = input.optString("name", "").trim()
            if (name.isEmpty()) throw ActionError("which app?")
            act(live, "open $name") { openApp(name) }
        }
        "open_url" -> {
            val url = input.optString("url", "").trim()
            if (!(url.startsWith("http://") || url.startsWith("https://"))) {
                throw ActionError("links must start with http:// or https://")
            }
            act(live, "open $url") { launch(Intent(Intent.ACTION_VIEW, Uri.parse(url)), url) }
        }
        "volume" -> volume(input, live)
        "media" -> {
            val a = input.optString("action", "").lowercase()
            if (a !in listOf("play_pause", "next", "previous", "stop")) throw ActionError("play_pause, next, previous or stop")
            act(live, "media: $a") { media(a) }
        }
        "lock" -> act(live, "lock the phone") { ControlService.need().lock(); JSONObject().put("locked", true) }
        "press" -> {
            val b = input.optString("button", "")
            act(live, "press $b") { JSONObject().put("pressed", ControlService.need().press(b)) }
        }
        "screen_text" -> JSONObject().put("screen", ControlService.need().screenText())  // read-only: runs even in dry run
        "tap_text" -> {
            val t = input.optString("text", "").trim()
            if (t.isEmpty()) throw ActionError("tap what?")
            act(live, "tap \"$t\"") { JSONObject().put("tapped", ControlService.need().tapText(t)) }
        }
        "tap" -> {
            val x = input.optInt("x", -1)
            val y = input.optInt("y", -1)
            if (x !in 0..100 || y !in 0..100) throw ActionError("x and y are 0 to 100 (percent of the screen)")
            act(live, "tap at $x%, $y%") { ControlService.need().tap(x, y); JSONObject().put("tapped", "$x%, $y%") }
        }
        "swipe" -> {
            val d = input.optString("direction", "")
            act(live, "swipe $d") { ControlService.need().swipe(d); JSONObject().put("swiped", d) }
        }
        "type" -> {
            val t = input.optString("text", "")
            if (t.isEmpty()) throw ActionError("type what?")
            act(live, "type ${t.take(40)}") {
                JSONObject().put("field_now", ControlService.need().type(t, input.optBoolean("replace", false)).take(200))
            }
        }
        else -> throw ActionError("the phone app doesn't know \"$workflow\" yet (update the app)")
    }

    private fun act(live: Boolean, would: String, block: () -> JSONObject): JSONObject =
        if (!live) JSONObject().put("dry_run", true).put("would", would) else block()

    private fun onOff(input: JSONObject): Boolean = when (input.optString("state", "").lowercase()) {
        "on", "true", "yes" -> true
        "off", "false", "no" -> false
        else -> throw ActionError("say on or off")
    }

    // ------------------------------------------------------------------ how the phone is doing

    private fun status(): JSONObject {
        val battery = ctx.registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
        val level = battery?.getIntExtra(BatteryManager.EXTRA_LEVEL, -1) ?: -1
        val scale = battery?.getIntExtra(BatteryManager.EXTRA_SCALE, 100) ?: 100
        val plugged = (battery?.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0) ?: 0) != 0
        val cm = ctx.getSystemService(ConnectivityManager::class.java)
        val caps = cm.getNetworkCapabilities(cm.activeNetwork)
        val network = when {
            caps == null -> "offline"
            caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) -> "wifi"
            caps.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR) -> "mobile data"
            else -> "other"
        }
        val fs = StatFs(Environment.getDataDirectory().path)
        val audio = ctx.getSystemService(AudioManager::class.java)
        val ringer = when (audio.ringerMode) {
            AudioManager.RINGER_MODE_SILENT -> "silent"
            AudioManager.RINGER_MODE_VIBRATE -> "vibrate"
            else -> "normal"
        }
        val dnd = ctx.getSystemService(NotificationManager::class.java).currentInterruptionFilter !=
            NotificationManager.INTERRUPTION_FILTER_ALL
        return JSONObject()
            .put("battery_percent", if (level >= 0) level * 100 / scale else JSONObject.NULL)
            .put("charging", plugged)
            .put("network", network)
            .put("free_storage_gb", Math.round(fs.availableBytes / 1e8) / 10.0)
            .put("ringer", ringer)
            .put("do_not_disturb", dnd)
    }

    // ------------------------------------------------------------------ doing things

    fun ring(): JSONObject {
        val audio = ctx.getSystemService(AudioManager::class.java)
        val before = audio.getStreamVolume(AudioManager.STREAM_ALARM)
        audio.setStreamVolume(AudioManager.STREAM_ALARM, audio.getStreamMaxVolume(AudioManager.STREAM_ALARM), 0)
        val uri = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM)
            ?: RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE)
        val player = MediaPlayer().apply {
            setAudioAttributes(AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_ALARM)
                .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build())
            setDataSource(ctx, uri)
            isLooping = true
            prepare()
            start()
        }
        Handler(Looper.getMainLooper()).postDelayed({
            runCatching { player.stop(); player.release() }
            audio.setStreamVolume(AudioManager.STREAM_ALARM, before, 0)
        }, 20_000)
        return JSONObject().put("ringing_seconds", 20)
    }

    private fun volume(input: JSONObject, live: Boolean): JSONObject {
        val name = input.optString("stream", "media").lowercase().ifBlank { "media" }
        val stream = when (name) {
            "media", "music" -> AudioManager.STREAM_MUSIC
            "ring", "ringer" -> AudioManager.STREAM_RING
            "alarm" -> AudioManager.STREAM_ALARM
            "notification", "notifications" -> AudioManager.STREAM_NOTIFICATION
            else -> throw ActionError("volume of: media, ring, alarm or notification")
        }
        val level = if (input.has("level") && !input.isNull("level")) input.optInt("level", -1) else -1
        val change = input.optString("change", "").lowercase()
        if (level !in -1..100) throw ActionError("the level is 0 to 100")
        if (level < 0 && change !in listOf("up", "down", "mute", "unmute")) throw ActionError("say a level, or up, down, mute, unmute")
        return act(live, "set the phone's $name volume ${if (level >= 0) "to $level" else change}") {
            val audio = ctx.getSystemService(AudioManager::class.java)
            val max = audio.getStreamMaxVolume(stream)
            try {
                when {
                    level >= 0 -> audio.setStreamVolume(stream, Math.round(max * level / 100f), AudioManager.FLAG_SHOW_UI)
                    change == "mute" -> audio.adjustStreamVolume(stream, AudioManager.ADJUST_MUTE, AudioManager.FLAG_SHOW_UI)
                    change == "unmute" -> audio.adjustStreamVolume(stream, AudioManager.ADJUST_UNMUTE, AudioManager.FLAG_SHOW_UI)
                    else -> {
                        val step = maxOf(1, Math.round(max * 0.2f))
                        val now = audio.getStreamVolume(stream) + if (change == "up") step else -step
                        audio.setStreamVolume(stream, now.coerceIn(0, max), AudioManager.FLAG_SHOW_UI)
                    }
                }
            } catch (e: SecurityException) {
                throw ActionError("Android won't let apps change that volume while Do Not Disturb is on (allow Do Not Disturb access for Argus)")
            }
            JSONObject().put("stream", name).put("volume_percent", Math.round(audio.getStreamVolume(stream) * 100f / max))
        }
    }

    private fun media(action: String): JSONObject {
        val audio = ctx.getSystemService(AudioManager::class.java)
        val code = when (action) {
            "next" -> KeyEvent.KEYCODE_MEDIA_NEXT
            "previous" -> KeyEvent.KEYCODE_MEDIA_PREVIOUS
            "stop" -> KeyEvent.KEYCODE_MEDIA_STOP
            else -> KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE
        }
        audio.dispatchMediaKeyEvent(KeyEvent(KeyEvent.ACTION_DOWN, code))
        audio.dispatchMediaKeyEvent(KeyEvent(KeyEvent.ACTION_UP, code))
        return JSONObject().put("media", action)
    }

    private fun dnd(on: Boolean): JSONObject {
        val nm = ctx.getSystemService(NotificationManager::class.java)
        if (!nm.isNotificationPolicyAccessGranted) {
            throw ActionError("Allow Do Not Disturb access for Argus first (open the Argus app > Permissions)")
        }
        nm.setInterruptionFilter(if (on) NotificationManager.INTERRUPTION_FILTER_PRIORITY
                                 else NotificationManager.INTERRUPTION_FILTER_ALL)
        return JSONObject().put("do_not_disturb", on)
    }

    private fun torch(on: Boolean): JSONObject {
        val cam = ctx.getSystemService(CameraManager::class.java)
        val id = cam.cameraIdList.firstOrNull {
            cam.getCameraCharacteristics(it).get(CameraCharacteristics.FLASH_INFO_AVAILABLE) == true
        } ?: throw ActionError("this phone has no torch")
        cam.setTorchMode(id, on)
        return JSONObject().put("torch", on)
    }

    private fun timer(minutes: Int, label: String): JSONObject {
        val i = Intent(AlarmClock.ACTION_SET_TIMER)
            .putExtra(AlarmClock.EXTRA_LENGTH, minutes * 60)
            .putExtra(AlarmClock.EXTRA_SKIP_UI, true)
        if (label.isNotEmpty()) i.putExtra(AlarmClock.EXTRA_MESSAGE, label)
        return launch(i, "a $minutes minute timer").put("minutes", minutes)
    }

    private fun openApp(name: String): JSONObject {
        val pm = ctx.packageManager
        val apps = pm.queryIntentActivities(Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER), 0)
        val want = name.lowercase()
        val hit = apps.firstOrNull { it.loadLabel(pm).toString().lowercase() == want }
            ?: apps.firstOrNull { it.loadLabel(pm).toString().lowercase().startsWith(want) }
            ?: apps.firstOrNull { it.loadLabel(pm).toString().lowercase().contains(want) }
            ?: throw ActionError("no app called \"$name\" on the phone")
        val label = hit.loadLabel(pm).toString()
        val i = pm.getLaunchIntentForPackage(hit.activityInfo.packageName) ?: throw ActionError("$label can't be opened")
        return launch(i, label).put("app", label)
    }

    /**
     * Android only lets an app open screens from the background when it may "display over other apps". Without
     * that, a notification asks you to tap.
     */
    private fun launch(intent: Intent, what: String): JSONObject {
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        if (Settings.canDrawOverlays(ctx)) {
            ctx.startActivity(intent)
            return JSONObject().put("opened", what)
        }
        val pi = PendingIntent.getActivity(ctx, what.hashCode(), intent,
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val n = Notification.Builder(ctx, WorkerService.CHANNEL_ASK)
            .setSmallIcon(android.R.drawable.ic_menu_view)
            .setContentTitle("Ari: open $what?")
            .setContentText("Tap to open (allow Argus to display over other apps to skip this)")
            .setContentIntent(pi).setAutoCancel(true).build()
        ctx.getSystemService(NotificationManager::class.java).notify(what.hashCode(), n)
        return JSONObject().put("opened", false).put("tap_to_open", what)
    }
}
