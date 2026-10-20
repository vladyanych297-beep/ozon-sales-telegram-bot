package ru.ozonplan.app

import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URI
import java.security.MessageDigest

data class AvailableUpdate(val code: Int, val name: String, val size: Long, val sha256: String)

enum class UpdateStage { CHECKING, DOWNLOADING, VERIFYING, SAVING, READY }

data class UpdateProgress(
    val stage: UpdateStage, val bytes: Long = 0, val total: Long = 0, val attempt: Int = 1,
) {
    val fraction: Float get() = if (total > 0) (bytes.toDouble() / total).coerceIn(0.0, 1.0).toFloat() else 0f
    val percent: Int get() = (fraction * 100).toInt()
    val label: String get() = when (stage) {
        UpdateStage.CHECKING -> "Проверяем наличие новой версии…"
        UpdateStage.DOWNLOADING -> if (attempt > 1) "Повторяем загрузку части файла (попытка $attempt из 3)…"
            else "Скачиваем обновление…"
        UpdateStage.VERIFYING -> "Проверяем целостность файла…"
        UpdateStage.SAVING -> "Сохраняем обновление…"
        UpdateStage.READY -> "Скачивание и проверка завершены — 100%"
    }
    val downloadLabel: String get() = java.lang.String.format(java.util.Locale.forLanguageTag("ru"),
        "%d%% · %.1f / %.1f МБ", percent, bytes / 1048576.0, total / 1048576.0)
}

fun parseAvailableUpdate(raw: String): AvailableUpdate {
    val json = JSONObject(raw)
    val code = json.getInt("version_code")
    val name = json.getString("version_name")
    val size = json.getLong("size_bytes")
    val digest = json.getString("sha256")
    require(code > 0 && name.matches(Regex("[0-9A-Za-z._-]{1,40}")))
    require(size in 1..100_000_000 && digest.matches(Regex("[a-f0-9]{64}")))
    return AvailableUpdate(code, name, size, digest)
}

private fun updateConnection(base: String, path: String,
                             allowLocalHttp: Boolean = BuildConfig.DEBUG): HttpURLConnection =
    (URI("${normalizeServer(base, allowLocalHttp)}$path").toURL().openConnection() as HttpURLConnection).apply {
        connectTimeout = 15_000
        readTimeout = 120_000
        instanceFollowRedirects = false
    }

fun fetchAvailableUpdate(base: String): AvailableUpdate? {
    val connection = updateConnection(base, "/api/v1/update")
    try {
        connection.setRequestProperty("Accept", "application/json")
        return when (val status = connection.responseCode) {
            200 -> parseAvailableUpdate(connection.inputStream.bufferedReader(Charsets.UTF_8).use { it.readText() })
            404 -> null // Older servers and servers without a published release.
            else -> throw ApiException(status, "Не удалось проверить обновление ($status)")
        }
    } finally { connection.disconnect() }
}

private fun File.sha256(): String {
    val digest = MessageDigest.getInstance("SHA-256")
    inputStream().use { source ->
        val buffer = ByteArray(64 * 1024)
        while (true) {
            val count = source.read(buffer)
            if (count < 0) break
            digest.update(buffer, 0, count)
        }
    }
    return digest.digest().joinToString("") { "%02x".format(it) }
}

fun updateFile(filesDir: File): File = File(File(filesDir, "updates"), "ozon-plan-update.apk")

private const val UPDATE_PART_SIZE = 512 * 1024L
private const val UPDATE_PART_ATTEMPTS = 3

private fun downloadPart(base: String, start: Long, end: Long, total: Long,
                         allowLocalHttp: Boolean, onProgress: (UpdateProgress) -> Unit): ByteArray {
    val expectedLength = (end - start + 1).toInt()
    var lastError: IOException? = null
    repeat(UPDATE_PART_ATTEMPTS) { attempt ->
        onProgress(UpdateProgress(UpdateStage.DOWNLOADING, start, total, attempt + 1))
        val connection = updateConnection(base, "/api/v1/update/apk", allowLocalHttp)
        try {
            connection.readTimeout = 45_000
            connection.setRequestProperty("Range", "bytes=$start-$end")
            connection.setRequestProperty("Accept-Encoding", "identity")
            val status = connection.responseCode
            if (status != 206) {
                if (status in listOf(429, 500, 502, 503, 504) && attempt + 1 < UPDATE_PART_ATTEMPTS) {
                    lastError = IOException("Сервер временно недоступен ($status)")
                    return@repeat
                }
                throw ApiException(status, "Сервер не отдал часть обновления ($status)")
            }
            val expectedRange = "bytes $start-$end/$total"
            if (connection.getHeaderField("Content-Range") != expectedRange) {
                throw IOException("Неверный диапазон ответа сервера")
            }
            val bytes = ByteArrayOutputStream(expectedLength)
            connection.inputStream.use { source ->
                val buffer = ByteArray(64 * 1024)
                while (true) {
                    val count = source.read(buffer)
                    if (count < 0) break
                    if (bytes.size() + count > expectedLength) throw IOException("Часть обновления слишком велика")
                    bytes.write(buffer, 0, count)
                    onProgress(UpdateProgress(UpdateStage.DOWNLOADING, start + bytes.size(), total, attempt + 1))
                }
            }
            if (bytes.size() != expectedLength) throw IOException("Часть обновления загружена не полностью")
            return bytes.toByteArray()
        } catch (ex: IOException) {
            lastError = ex
        } finally {
            connection.disconnect()
        }
    }
    throw IOException("Не удалось загрузить часть обновления после $UPDATE_PART_ATTEMPTS попыток", lastError)
}

fun downloadAvailableUpdate(base: String, info: AvailableUpdate, filesDir: File,
                            allowLocalHttp: Boolean = BuildConfig.DEBUG,
                            onProgress: (UpdateProgress) -> Unit = {}): File {
    val target = updateFile(filesDir)
    target.parentFile?.mkdirs()
    if (target.isFile && target.length() == info.size) {
        onProgress(UpdateProgress(UpdateStage.VERIFYING, info.size, info.size))
        if (target.sha256() == info.sha256) {
            onProgress(UpdateProgress(UpdateStage.READY, info.size, info.size))
            return target
        }
    }
    onProgress(UpdateProgress(UpdateStage.DOWNLOADING, 0, info.size))
    val temporary = File(target.parentFile, "ozon-plan-update.tmp")
    try {
        val digest = MessageDigest.getInstance("SHA-256")
        temporary.outputStream().use { destination ->
            var offset = 0L
            while (offset < info.size) {
                val end = minOf(offset + UPDATE_PART_SIZE, info.size) - 1
                val part = downloadPart(base, offset, end, info.size, allowLocalHttp, onProgress)
                destination.write(part)
                digest.update(part)
                offset += part.size
            }
        }
        onProgress(UpdateProgress(UpdateStage.VERIFYING, info.size, info.size))
        val actual = digest.digest().joinToString("") { "%02x".format(it) }
        require(actual == info.sha256) { "Проверка обновления не прошла" }
        onProgress(UpdateProgress(UpdateStage.SAVING, info.size, info.size))
        if (target.exists()) require(target.delete()) { "Не удалось заменить старое обновление" }
        require(temporary.renameTo(target)) { "Не удалось сохранить обновление" }
        onProgress(UpdateProgress(UpdateStage.READY, info.size, info.size))
        return target
    } finally {
        temporary.delete()
    }
}
