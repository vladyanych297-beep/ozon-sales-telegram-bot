package ru.ozonplan.app

import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URI

class ApiException(val status: Int, message: String) : IllegalStateException(message)

fun serverRequest(base: String, path: String, token: String? = null, body: JSONObject? = null): String {
    val address = normalizeServer(base, BuildConfig.DEBUG)
    val connection = URI("$address$path").toURL().openConnection() as HttpURLConnection
    connection.connectTimeout = 15_000
    connection.readTimeout = when {
        path.startsWith("/api/v1/profitability?") -> 600_000
        path.startsWith("/api/") -> 190_000
        else -> 20_000
    }
    connection.instanceFollowRedirects = false
    connection.setRequestProperty("Accept", "application/json")
    token?.let { connection.setRequestProperty("Authorization", "Bearer $it") }
    try {
        if (body != null) {
            connection.requestMethod = "POST"
            connection.doOutput = true
            connection.setRequestProperty("Content-Type", "application/json; charset=utf-8")
            connection.outputStream.use { it.write(body.toString().toByteArray(Charsets.UTF_8)) }
        }
        val status = connection.responseCode
        val stream = if (status == 200) connection.inputStream else connection.errorStream
        val bytes = stream?.use { input ->
            val out = java.io.ByteArrayOutputStream()
            val buffer = ByteArray(8192)
            while (true) {
                val count = input.read(buffer)
                if (count == -1) break
                require(out.size() + count <= 20 * 1024 * 1024) { "Ответ сервера слишком большой" }
                out.write(buffer, 0, count)
            }
            out.toByteArray()
        } ?: byteArrayOf()
        val text = bytes.toString(Charsets.UTF_8)
        if (status != 200) {
            val detail = runCatching { JSONObject(text).getString("detail") }.getOrNull()
            throw ApiException(status, detail?.take(500) ?: "Сервер вернул ошибку $status")
        }
        return text
    } finally { connection.disconnect() }
}
