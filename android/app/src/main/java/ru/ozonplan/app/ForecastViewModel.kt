package ru.ozonplan.app

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.File

data class ScreenState(
    val server: String = "", val period: String = "30", val salesPeriod: String = "30",
    val query: String = "",
    val onlyNeeded: Boolean = false, val report: Report? = null,
    val cachedReport: Boolean = false,
    val clusterMetrics: Set<ClusterMetric> = ClusterMetric.entries.toSet(),
    val loading: Boolean = false, val error: String? = null,
    val updateVersion: String? = null, val updateDownloading: Boolean = false,
    val updateReady: Boolean = false, val updateError: String? = null,
    val updateProgress: UpdateProgress? = null, val updateInstallStatus: String? = null,
) {
    val days: Int? get() = period.toIntOrNull()?.takeIf { it in 1..365 }
    val salesDays: Int? get() = salesPeriod.toIntOrNull()?.takeIf { it in 1..365 }
    val filtered: List<ProductRow> get() {
        val applied = report ?: return emptyList()
        return applied.rows.filter { row ->
            (query.isBlank() || listOf(row.name, row.offerId, *row.skus.toTypedArray())
                .any { it.contains(query.trim(), ignoreCase = true) }) &&
                (!onlyNeeded || row.toSupply(
                    applied.forecastPeriodDays, applied.salesPeriodDays
                ) > 0)
        }.sortedWith(compareByDescending<ProductRow> {
            it.toSupply(applied.forecastPeriodDays, applied.salesPeriodDays)
        }.thenByDescending { it.sales }.thenBy { it.name })
    }
}

class ForecastViewModel(application: Application) : AndroidViewModel(application) {
    private val preferences = application.getSharedPreferences("forecast", 0)
    private val initialServer = preferences.getString("server", "") ?: ""
    private val mutable = MutableStateFlow(ScreenState(
        server = initialServer,
        clusterMetrics = selectedClusterMetrics(preferences.getStringSet("cluster_metrics", null)),
        period = preferences.getInt("days", 30).coerceIn(1, 365).toString(),
        salesPeriod = preferences.getInt("sales_days", 30).coerceIn(1, 365).toString(),
    ))
    val state = mutable.asStateFlow()
    private var request: Job? = null
    private var generation = 0
    private var updateJob: Job? = null
    private var lastUpdateCheck = 0L
    private var updateEpoch = 0

    private val reportCache = File(application.filesDir, "last-report.json")

    init {
        loadCachedReport(initialServer)?.let { report ->
            mutable.update { it.copy(report = report, cachedReport = true) }
        }
    }

    fun onForeground() {
        // Forecast data is fetched by Refresh alone.
        checkForUpdates()
    }

    fun onBackground() {
        cancelRequest()
        mutable.update { it.copy(loading = false) }
    }

    private fun cancelRequest() { generation++; request?.cancel(); request = null }

    fun setPeriod(value: String) {
        if (value.length > 3 || value.any { !it.isDigit() }) return
        mutable.update { it.copy(period = value) }
        value.toIntOrNull()?.takeIf { it in 1..365 }?.let {
            preferences.edit().putInt("days", it).apply()
        }
    }
    fun setSalesPeriod(value: String) {
        if (value.length > 3 || value.any { !it.isDigit() }) return
        mutable.update { it.copy(salesPeriod = value) }
        value.toIntOrNull()?.takeIf { it in 1..365 }?.let {
            preferences.edit().putInt("sales_days", it).apply()
        }
    }
    fun search(value: String) { mutable.update { it.copy(query = value) } }
    fun filterNeeded(value: Boolean) { mutable.update { it.copy(onlyNeeded = value) } }

    fun toggleClusterMetric(metric: ClusterMetric) {
        mutable.update { state ->
            val selected = if (metric in state.clusterMetrics) state.clusterMetrics - metric
                else state.clusterMetrics + metric
            preferences.edit().putStringSet("cluster_metrics", selected.map { it.name }.toSet()).apply()
            state.copy(clusterMetrics = selected)
        }
    }

    fun setServer(value: String): String? {
        val address = try { normalizeServer(value, BuildConfig.DEBUG) }
        catch (ex: IllegalArgumentException) { return ex.message }
        cancelRequest()
        updateEpoch++
        updateJob?.cancel()
        updateJob = null
        lastUpdateCheck = 0L
        preferences.edit().putString("server", address).apply()
        val cached = loadCachedReport(address)
        mutable.update { it.copy(server = address, report = cached, cachedReport = cached != null,
            loading = false, error = null,
            updateVersion = null, updateReady = false, updateDownloading = false, updateError = null,
            updateProgress = null, updateInstallStatus = null) }
        checkForUpdates(force = true)
        return null
    }

    private fun runRequest(action: suspend (String, Int) -> Unit) {
        if (mutable.value.server.isBlank() || mutable.value.loading) return
        val address = mutable.value.server
        val current = generation
        request = viewModelScope.launch {
            mutable.update { it.copy(loading = true, error = null) }
            try {
                action(address, current)
            } catch (ex: CancellationException) {
                throw ex
            } catch (ex: Exception) {
                if (current == generation) {
                    mutable.update { state ->
                        val hasSavedReport = state.report != null
                        state.copy(
                            cachedReport = hasSavedReport,
                            error = when (ex) {
                                is javax.net.ssl.SSLException -> "Не удалось проверить HTTPS-сертификат сервера"
                                is java.io.IOException -> if (hasSavedReport) {
                                    "Нет связи с сервером. Показан последний сохранённый отчёт."
                                } else "Нет связи с сервером. Данные не обновлены."
                                is IllegalStateException -> ex.message
                                else -> "Не удалось обработать ответ сервера"
                            },
                        )
                    }
                }
            } finally {
                if (current == generation) mutable.update { it.copy(loading = false) }
            }
        }
    }

    private suspend fun loadReport(
        address: String, current: Int, days: Int, salesDays: Int,
    ) {
        val (report, raw) = withContext(Dispatchers.IO) {
            val raw = serverRequest(
                address, "/api/v1/forecast?days=$days&sales_days=$salesDays"
            )
            parseReport(raw) to raw
        }
        if (current == generation) {
            withContext(Dispatchers.IO) { saveCachedReport(address, raw) }
            if (current == generation) mutable.update {
                it.copy(report = report, cachedReport = false, error = null)
            }
        }
    }

    fun refresh() {
        val days = mutable.value.days ?: return
        val salesDays = mutable.value.salesDays ?: return
        runRequest { address, current -> loadReport(address, current, days, salesDays) }
    }

    fun checkForUpdates(force: Boolean = false) {
        val address = mutable.value.server.takeIf { it.isNotBlank() } ?: return
        val now = android.os.SystemClock.elapsedRealtime()
        if (updateJob?.isActive == true || (!force && lastUpdateCheck != 0L &&
                now - lastUpdateCheck < 6 * 60 * 60 * 1000L)) return
        lastUpdateCheck = now
        val epoch = updateEpoch
        updateJob = viewModelScope.launch {
            mutable.update { it.copy(updateError = null, updateReady = false,
                updateProgress = UpdateProgress(UpdateStage.CHECKING), updateInstallStatus = null) }
            try {
                val info = withContext(Dispatchers.IO) { fetchAvailableUpdate(address) }
                if (epoch != updateEpoch) return@launch
                if (info == null || info.code <= BuildConfig.VERSION_CODE) {
                    mutable.update { it.copy(updateProgress = null, updateVersion = null) }
                    return@launch
                }
                mutable.update { it.copy(updateVersion = info.name, updateDownloading = true, updateReady = false) }
                withContext(Dispatchers.IO) {
                    downloadAvailableUpdate(address, info, getApplication<Application>().filesDir) { progress ->
                        if (epoch == updateEpoch) mutable.update { it.copy(updateProgress = progress) }
                    }
                }
                if (epoch == updateEpoch) mutable.update {
                    it.copy(updateDownloading = false, updateReady = true)
                }
            } catch (ex: CancellationException) {
                throw ex
            } catch (ex: Exception) {
                if (epoch == updateEpoch) mutable.update {
                    it.copy(updateDownloading = false, updateReady = false, updateProgress = null,
                        updateError = if (it.updateVersion != null) when (ex) {
                            is ApiException -> "Не удалось скачать обновление (HTTP ${ex.status}). Повторите попытку."
                            is java.net.SocketTimeoutException -> "Истекло время загрузки. Повторите попытку."
                            is java.io.IOException -> "Соединение прервалось при загрузке. Повторите попытку."
                            is IllegalArgumentException -> "Файл обновления не прошёл проверку. Повторите попытку."
                            else -> "Не удалось скачать обновление. Повторите попытку."
                        } else "Не удалось проверить обновления. Повторите попытку.")
                }
            }
        }
    }

    fun updateInstallerOpened(status: String) {
        mutable.update { it.copy(updateInstallStatus = status) }
    }

    fun downloadedUpdate(): File? = updateFile(getApplication<Application>().filesDir)
        .takeIf { mutable.value.updateReady && it.isFile }

    private fun loadCachedReport(server: String): Report? {
        if (server.isBlank() || !reportCache.isFile) return null
        return runCatching {
            decodeReportCache(reportCache.readText(Charsets.UTF_8), server)
        }.getOrNull()
    }

    private fun saveCachedReport(server: String, raw: String) {
        runCatching {
            val temporary = File(reportCache.parentFile, "last-report.tmp")
            temporary.writeText(encodeReportCache(server, raw), Charsets.UTF_8)
            if (!temporary.renameTo(reportCache)) {
                temporary.copyTo(reportCache, overwrite = true)
                temporary.delete()
            }
        }
    }

}
