package dev.argus.companion

import org.json.JSONObject
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL

/** Argus's HTTP API with the worker token (the same calls the PC's Python worker makes). */
class Api(private val base: String, private val token: String) {

    class Reply(val status: Int, val body: String) {
        fun json(): JSONObject = if (body.isBlank()) JSONObject() else JSONObject(body)
    }

    @Throws(IOException::class)
    fun call(method: String, path: String, body: JSONObject? = null, timeoutMs: Int = 15_000): Reply {
        val c = URL(base + path).openConnection() as HttpURLConnection
        try {
            c.requestMethod = method
            c.connectTimeout = 10_000
            c.readTimeout = timeoutMs
            c.setRequestProperty("Accept", "application/json")
            if (token.isNotEmpty()) c.setRequestProperty("Authorization", "Bearer $token")
            if (body != null) {
                c.doOutput = true
                c.setRequestProperty("Content-Type", "application/json")
                c.outputStream.use { it.write(body.toString().toByteArray()) }
            }
            val code = c.responseCode
            val stream = if (code >= 400) c.errorStream else c.inputStream
            val text = stream?.bufferedReader()?.use { it.readText() } ?: ""
            return Reply(code, text)
        } finally {
            c.disconnect()
        }
    }

    fun post(path: String, body: JSONObject, timeoutMs: Int = 15_000) = call("POST", path, body, timeoutMs)
}
