package ru.ozonplan.app

import android.os.Bundle
import android.content.Intent
import android.content.ClipData
import android.provider.Settings
import android.net.Uri
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.compose.LifecycleStartEffect
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.core.content.FileProvider
import java.io.File
import java.time.OffsetDateTime
import java.time.LocalDate
import java.time.format.DateTimeFormatter

private val Blue = Color(0xFF1755E8)
private val Ink = Color(0xFF18243B)
private val Muted = Color(0xFF65718A)
private val Canvas = Color(0xFFF5F7FB)

class MainActivity : ComponentActivity() {
    fun installUpdate(file: File): String {
        if (!packageManager.canRequestPackageInstalls()) {
            startActivity(Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                Uri.parse("package:$packageName")))
            return "Открыты настройки Android. Разрешите установку, вернитесь сюда и нажмите «Установить обновление»."
        }
        val info = packageManager.getPackageArchiveInfo(file.absolutePath, 0)
        require(info?.packageName == packageName) { "Файл обновления не принадлежит приложению" }
        val uri = FileProvider.getUriForFile(this, "$packageName.updates", file)
        startActivity(Intent(Intent.ACTION_INSTALL_PACKAGE).apply {
            data = uri
            clipData = ClipData.newUri(contentResolver, "Обновление", uri)
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        })
        return "Файл передан установщику Android. Подтвердите установку; дальнейший прогресс показывает Android."
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // Keep the display awake only while this activity is visible.
        // Screenshots are allowed at the user's request.
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        enableEdgeToEdge()
        setContent {
            MaterialTheme(colorScheme = lightColorScheme(primary = Blue, background = Canvas,
                surface = Color.White, onSurface = Ink, onBackground = Ink)) {
                ForecastScreen()
            }
        }
    }
}

@Composable
fun ForecastScreen(model: ForecastViewModel = viewModel()) {
    val state by model.state.collectAsStateWithLifecycle()
    var profitability by rememberSaveable { mutableStateOf(false) }
    var settings by rememberSaveable { mutableStateOf(false) }
    val context = LocalContext.current
    var openError by remember { mutableStateOf<String?>(null) }
    var deferredUpdate by rememberSaveable { mutableStateOf<String?>(null) }
    val visibleRows = state.filtered
    LifecycleStartEffect(model) {
        model.onForeground()
        onStopOrDispose { model.onBackground() }
    }
    if (profitability) {
        ProfitabilityScreen(state.server, onBack = { profitability = false })
        return
    }
    Scaffold(containerColor = Canvas, bottomBar = {
        Text("© ESI.Company · ${BuildConfig.VERSION_NAME}", color = Muted, fontSize = 12.sp,
            textAlign = TextAlign.Center,
            modifier = Modifier.fillMaxWidth().navigationBarsPadding().padding(vertical = 8.dp))
    }) { insets ->
        LazyColumn(Modifier.fillMaxSize().padding(insets),
            contentPadding = PaddingValues(start = 20.dp, end = 20.dp, bottom = 16.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp)) {
            item(key = "controls", contentType = "controls") {
                Column {
                    Row(Modifier.fillMaxWidth().padding(top = 14.dp, bottom = 18.dp),
                        verticalAlignment = Alignment.CenterVertically) {
                        Column(Modifier.weight(1f)) {
                            Text("OZON / ПОПОЛНЕНИЕ", color = Blue, fontSize = 11.sp,
                                fontWeight = FontWeight.Bold, letterSpacing = 1.4.sp)
                            Text("План поставок", fontSize = 27.sp, fontWeight = FontWeight.Bold)
                        }
                        TextButton(onClick = { settings = true }) { Text("Сервер") }
                    }
                    TextButton(onClick = { profitability = true }, enabled = state.server.isNotBlank()) { Text("Рентабельность →") }
                    if (state.server.isBlank()) {
                        Spacer(Modifier.height(30.dp))
                        Text("Подключите магазин", fontSize = 24.sp, fontWeight = FontWeight.Bold)
                        Spacer(Modifier.height(12.dp))
                        Text("Введите адрес серверного компьютера. Приложение получит продажи, остатки и товары в пути из одного магазина Ozon.", color = Muted)
                        Spacer(Modifier.height(24.dp))
                        Button(onClick = { settings = true }, modifier = Modifier.fillMaxWidth().height(52.dp)) {
                            Text("Настроить подключение")
                        }
                        Spacer(Modifier.height(16.dp))
                        Text("Ключ Ozon хранится только на сервере. Отчёт доступен каждому, кто знает адрес сервера.",
                            color = Muted, fontSize = 13.sp)
                    } else {
                        if (state.updateVersion != null || state.updateProgress != null || state.updateError != null) {
                            Surface(color = Color(0xFFE2EDFF), shape = RoundedCornerShape(14.dp),
                                modifier = Modifier.fillMaxWidth().padding(bottom = 12.dp)) {
                                Column(Modifier.padding(14.dp)) {
                                    Text(state.updateVersion?.let { "Доступна новая версия $it" } ?: "Обновление приложения", fontWeight = FontWeight.SemiBold)
                                    val progress = state.updateProgress
                                    if (progress != null && !state.updateReady) {
                                        Text(progress.label, color = Muted, fontSize = 12.sp)
                                        if (progress.stage == UpdateStage.DOWNLOADING) {
                                            Text(progress.downloadLabel, color = Ink, fontSize = 13.sp,
                                                fontWeight = FontWeight.SemiBold)
                                            LinearProgressIndicator(progress = { progress.fraction },
                                                modifier = Modifier.fillMaxWidth().padding(top = 8.dp))
                                        } else {
                                            LinearProgressIndicator(Modifier.fillMaxWidth().padding(top = 8.dp))
                                        }
                                    } else if (state.updateReady) {
                                        Text(state.updateInstallStatus ?: "Скачивание и проверка завершены — 100%. Обновление готово к установке.",
                                            color = Muted, fontSize = 12.sp)
                                        Button(onClick = {
                                            try {
                                                val file = requireNotNull(model.downloadedUpdate())
                                                model.updateInstallerOpened((context as MainActivity).installUpdate(file))
                                                openError = null
                                            } catch (_: Exception) {
                                                openError = "Не удалось открыть установку обновления. Проверьте разрешение установки для приложения."
                                            }
                                        }) { Text("Установить обновление") }
                                    } else if (state.updateError != null) {
                                        Text(state.updateError ?: "", color = Muted, fontSize = 12.sp)
                                        TextButton(onClick = { model.checkForUpdates(force = true) }) { Text("Повторить") }
                                    }
                                }
                            }
                        }
                        openError?.let { Notice(it) }
                        Row(verticalAlignment = Alignment.CenterVertically,
                            horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                            OutlinedTextField(value = state.salesPeriod,
                                onValueChange = model::setSalesPeriod,
                                label = { Text("Продажи, дней") }, singleLine = true,
                                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                                isError = state.salesDays == null, modifier = Modifier.weight(1f),
                                shape = RoundedCornerShape(14.dp))
                            OutlinedTextField(value = state.period, onValueChange = model::setPeriod,
                                label = { Text("Прогноз, дней") }, singleLine = true,
                                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                                isError = state.days == null, modifier = Modifier.weight(1f),
                                shape = RoundedCornerShape(14.dp))
                        }
                        if (state.days == null || state.salesDays == null) {
                            Text("Для обоих периодов введите от 1 до 365 дней",
                                color = MaterialTheme.colorScheme.error, fontSize = 12.sp)
                        }
                        Button(onClick = model::refresh,
                            enabled = !state.loading && state.days != null && state.salesDays != null,
                            modifier = Modifier.fillMaxWidth().height(52.dp).padding(top = 8.dp),
                            shape = RoundedCornerShape(14.dp)) {
                            Text(if (state.loading) "Загрузка…" else "Обновить")
                        }
                        Spacer(Modifier.height(12.dp))
                        if (state.loading) LinearProgressIndicator(Modifier.fillMaxWidth())
                        val report = state.report
                        if (report != null) {
                            val total = report.rows.sumOf {
                                it.toSupply(report.forecastPeriodDays, report.salesPeriodDays)
                            }
                            Surface(color = Blue, shape = RoundedCornerShape(18.dp), modifier = Modifier.fillMaxWidth()) {
                                Column(Modifier.padding(18.dp)) {
                                    Text("К ПОСТАВКЕ НА ${report.forecastPeriodDays} ДНЕЙ",
                                        color = Color.White.copy(alpha = 0.75f), fontSize = 11.sp,
                                        letterSpacing = 1.sp)
                                    Row(verticalAlignment = Alignment.Bottom) {
                                        Text(total.toString(), color = Color.White, fontSize = 34.sp,
                                            fontWeight = FontWeight.Bold)
                                        Text("  шт.", color = Color.White, modifier = Modifier.padding(bottom = 6.dp))
                                    }
                                    Text("${report.rows.size} товаров · все склады Ozon", color = Color.White, fontSize = 12.sp)
                                }
                            }
                            Spacer(Modifier.height(10.dp))
                            Text("Продажи: ${dateLabel(report.dateFrom)} — ${dateLabel(report.dateTo)} · FBO + FBS", color = Muted, fontSize = 11.sp)
                            Text("Загружено: ${timeLabel(report.fetchedAt)} · ${report.timezone}", color = Muted, fontSize = 11.sp)
                            if (state.cachedReport && !report.stale) {
                                Notice("Показан последний сохранённый отчёт. Нажмите «Обновить», чтобы получить актуальные данные.")
                            }
                            if (report.stale) {
                                Notice("Сохранённые данные — обновление не подтверждено. Расчёт использует прежние остатки.")
                            }
                            report.warning?.let { if (state.error == null) Notice(it) }
                        }
                        state.error?.let { Notice(it) }
                        if (report == null && !state.loading && state.error == null) {
                            Text("Нажмите «Обновить», чтобы получить данные магазина.", modifier = Modifier.padding(vertical = 20.dp), color = Muted)
                        }
                        if (report != null) {
                            OutlinedTextField(value = state.query, onValueChange = model::search,
                                placeholder = { Text("Название, артикул или SKU") }, singleLine = true,
                                modifier = Modifier.fillMaxWidth().padding(top = 14.dp), shape = RoundedCornerShape(14.dp))
                            Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth()) {
                                Text("Только к поставке", modifier = Modifier.weight(1f), fontSize = 13.sp)
                                Switch(checked = state.onlyNeeded, onCheckedChange = model::filterNeeded)
                            }
                            ClusterDisplayMenu(state.clusterMetrics, model::toggleClusterMetric)
                            if (visibleRows.isEmpty()) {
                                Text(if (report.rows.isEmpty()) "В магазине пока нет товаров" else "По выбранному фильтру товаров нет",
                                    color = Muted, modifier = Modifier.padding(vertical = 24.dp))
                            }
                        } else if (state.loading) {
                            Text("Получаем каталог, продажи за ${state.salesDays ?: "—"} дней и остатки. Первая загрузка может занять несколько минут.",
                                modifier = Modifier.padding(vertical = 20.dp), color = Muted)
                        }
                    }
                }
            }
            if (state.server.isNotBlank()) {
                items(visibleRows, key = { "product:${it.id}" }, contentType = { "product" }) { row ->
                    state.report?.let { ProductCard(row, it.forecastPeriodDays, it.salesPeriodDays, state.clusterMetrics) }
                }
            }
        }
    }
    if (settings) ServerDialog(state.server, onDismiss = { settings = false }, onSave = model::setServer)
    if (state.updateReady && state.updateVersion != deferredUpdate) {
        AlertDialog(onDismissRequest = { deferredUpdate = state.updateVersion },
            title = { Text("Обновление готово") },
            text = { Text("Версия ${state.updateVersion} скачана и проверена. Android попросит подтвердить установку.") },
            confirmButton = {
                TextButton(onClick = {
                    deferredUpdate = state.updateVersion
                    try {
                        val file = requireNotNull(model.downloadedUpdate())
                        model.updateInstallerOpened((context as MainActivity).installUpdate(file))
                        openError = null
                    } catch (_: Exception) {
                        openError = "Не удалось открыть установку обновления. Проверьте разрешение установки для приложения."
                    }
                }) { Text("Установить") }
            },
            dismissButton = {
                TextButton(onClick = { deferredUpdate = state.updateVersion }) { Text("Позже") }
            })
    }
}

@Composable
private fun Notice(text: String) {
    Surface(color = Color(0xFFFFF0D4), shape = RoundedCornerShape(10.dp),
        modifier = Modifier.fillMaxWidth().padding(top = 8.dp)) {
        Text(text, modifier = Modifier.padding(10.dp), color = Color(0xFF74501A), fontSize = 12.sp)
    }
}

@Composable
private fun ProductCard(
    row: ProductRow, forecastDays: Int, salesDays: Int, metrics: Set<ClusterMetric>,
) {
    var expanded by rememberSaveable(row.id) { mutableStateOf(false) }
    Surface(shape = RoundedCornerShape(16.dp), modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp)) {
            Text(row.name, fontSize = 15.sp, fontWeight = FontWeight.SemiBold)
            Text(row.offerId, color = Muted, fontSize = 11.sp, modifier = Modifier.padding(top = 3.dp))
            Spacer(Modifier.height(15.dp))
            Row(Modifier.fillMaxWidth()) {
                Metric("Продажи / ${salesDays}д", row.sales.toString(), Modifier.weight(1.1f))
                Metric("Остаток", row.stock.toString(), Modifier.weight(1f))
                Metric("В пути", row.inTransit.toString(), Modifier.weight(1f))
                Metric("К поставке", row.toSupply(forecastDays, salesDays).toString(),
                    Modifier.weight(1.1f), true)
            }
            StockDaysLabel(calculateStockDays(row.sales, row.stock, salesDays))
            TextButton(onClick = { expanded = !expanded },
                modifier = Modifier.fillMaxWidth().padding(top = 8.dp)) {
                Text(if (expanded) "Скрыть данные по кластерам ▲" else "Продажи в кластеры и остатки ▼")
            }
            if (expanded) {
                HorizontalDivider(color = Canvas)
                if (row.clusters.isEmpty()) {
                    Text("Разбивка по кластерам ещё не загружена. Нажмите «Обновить».",
                        color = Muted, fontSize = 12.sp, modifier = Modifier.padding(top = 10.dp))
                } else {
                    row.clusters.forEach { cluster ->
                        Column(Modifier.fillMaxWidth().padding(top = 12.dp)) {
                            Text("${cluster.name} · ${cluster.scheme}", fontSize = 13.sp,
                                fontWeight = FontWeight.Medium)
                            val quantities = clusterMetricsText(cluster.sales, cluster.stock,
                                cluster.inTransit, metrics)
                            if (quantities.isNotEmpty()) {
                                Text(quantities, color = Muted, fontSize = 12.sp)
                            }
                            if (ClusterMetric.TO_SUPPLY in metrics) {
                                Text("К поставке на ${forecastDays}д: ${cluster.toSupply(forecastDays, salesDays)} шт.",
                                    color = Blue, fontSize = 13.sp, fontWeight = FontWeight.Bold,
                                    modifier = Modifier.padding(top = 4.dp))
                            }
                            if (ClusterMetric.STOCK_DAYS in metrics) {
                                StockDaysLabel(cluster.stockDays(salesDays))
                            }
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun StockDaysLabel(days: Double?) {
    val value = formatStockDays(days)
    Text(if (value == null) "Запас в днях: нет продаж" else "Запас текущего остатка: $value",
        color = if (days != null && days < 7) Color(0xFFB3261E) else Muted,
        fontSize = 12.sp, modifier = Modifier.padding(top = 6.dp))
}

@Composable
private fun ClusterDisplayMenu(selected: Set<ClusterMetric>, onToggle: (ClusterMetric) -> Unit) {
    var expanded by remember { mutableStateOf(false) }
    Box {
        OutlinedButton(onClick = { expanded = true }) { Text("Показатели кластеров ▾") }
        DropdownMenu(expanded = expanded, onDismissRequest = { expanded = false },
            modifier = Modifier.width(300.dp)) {
            ClusterMetric.entries.forEach { metric ->
                DropdownMenuItem(text = { Text(metric.label) },
                    leadingIcon = { Checkbox(checked = metric in selected, onCheckedChange = null) },
                    onClick = { onToggle(metric) })
            }
        }
    }
}

internal fun clusterMetricsText(
    sales: Long, stock: Long, inTransit: Long,
    selected: Set<ClusterMetric> = ClusterMetric.entries.toSet(),
) = buildAnnotatedString {
    val values = listOf(
        Triple(ClusterMetric.SALES, "Продажи в кластер", sales),
        Triple(ClusterMetric.STOCK, "Остаток", stock),
        Triple(ClusterMetric.IN_TRANSIT, "В пути", inTransit),
    ).filter { it.first in selected }
    values.forEachIndexed { index, (_, label, value) ->
        if (index > 0) append(" · ")
        append("$label: ")
        metricValue(value)
    }
}

private fun androidx.compose.ui.text.AnnotatedString.Builder.metricValue(value: Long) {
    if (value != 0L) {
        withStyle(SpanStyle(fontWeight = FontWeight.Bold)) { append(value.toString()) }
    } else {
        append("0")
    }
}

@Composable
private fun Metric(label: String, value: String, modifier: Modifier, accent: Boolean = false) {
    Column(modifier, horizontalAlignment = Alignment.CenterHorizontally) {
        Text(label, fontSize = 10.sp, color = Muted, textAlign = TextAlign.Center)
        Text(value, fontSize = 21.sp, fontWeight = FontWeight.Bold,
            color = if (accent) Blue else Ink, modifier = Modifier.padding(top = 5.dp))
    }
}

@Composable
private fun ServerDialog(current: String, onDismiss: () -> Unit, onSave: (String) -> String?) {
    var address by rememberSaveable { mutableStateOf(current) }
    var error by remember { mutableStateOf<String?>(null) }
    AlertDialog(onDismissRequest = onDismiss, title = { Text("Адрес сервера") }, text = {
        Column {
            Text("Администратор предоставит HTTPS-адрес после настройки компьютера и роутера.", fontSize = 13.sp, color = Muted)
            Spacer(Modifier.height(16.dp))
            OutlinedTextField(value = address, onValueChange = { address = it; error = null },
                label = { Text("https://…") }, keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri),
                singleLine = true, isError = error != null)
            error?.let { Text(it, color = MaterialTheme.colorScheme.error, fontSize = 12.sp) }
        }
    }, confirmButton = { TextButton(onClick = { error = onSave(address); if (error == null) onDismiss() }) { Text("Подключить") } },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Отмена") } })
}

private fun dateLabel(value: String): String = runCatching {
    LocalDate.parse(value).format(DateTimeFormatter.ofPattern("dd.MM.yyyy"))
}.getOrDefault(value)

private fun timeLabel(value: String): String = runCatching {
    OffsetDateTime.parse(value).format(DateTimeFormatter.ofPattern("dd.MM.yyyy HH:mm"))
}.getOrDefault(value)
