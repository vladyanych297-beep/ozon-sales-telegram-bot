package ru.ozonplan.app

import android.app.Application
import androidx.activity.compose.BackHandler
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.File
import java.math.BigDecimal
import java.math.RoundingMode
import java.security.MessageDigest
import java.time.LocalDate
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel

internal fun profitabilityRange(days: Int, today: LocalDate = LocalDate.now()): Pair<LocalDate, LocalDate> {
    require(days in 1..365)
    return today.minusDays(days.toLong() - 1) to today
}

internal fun costCents(text: String): Long? = runCatching {
    val number = text.trim().replace(',', '.').toBigDecimal()
    require(number >= BigDecimal.ZERO && number <= BigDecimal("1000000000"))
    number.movePointRight(2).longValueExact()
}.getOrNull()

internal fun currency(cents: Long): String = groupThousands(BigDecimal.valueOf(cents, 2).setScale(2).toPlainString()).replace('.', ',') + " ₽"

internal data class ProfitRow(val id: String, val offer: String, val name: String,
    val ordered: Long, val redeemed: Long, val returned: Long,
    val revenue: Long, val fees: Long, val cost: Long?,
    val buyerPaid: Long? = null, val points: Long? = null, val subsidy: Long? = null) {
    val profit: Long? get() = cost?.let { revenue - fees - it * (redeemed - returned) }
    val margin: String? get() = profit?.takeIf { revenue > 0 }?.let {
        BigDecimal.valueOf(it).multiply(BigDecimal(100)).divide(BigDecimal.valueOf(revenue), 2, RoundingMode.HALF_UP).toPlainString() + " %"
    }
    val costThreshold: Long? get() = cost?.let { unitCost -> averageFee?.let { it + unitCost } }
    val averageFee: Long? get() = if (ordered == 0L) null else
        BigDecimal.valueOf(fees).divide(BigDecimal.valueOf(ordered), 0, RoundingMode.HALF_UP).longValueExact()
}

internal fun parseProfitRows(json: JSONObject): List<ProfitRow> {
    val rows = json.getJSONArray("rows")
    return (0 until rows.length()).map { index ->
        val r = rows.getJSONObject(index)
        ProfitRow(r.getString("product_id"), r.getString("offer_id"), r.getString("name"),
            r.getLong("ordered"), r.getLong("redeemed"), r.getLong("returned"),
            r.getLong("revenue_cents"), r.getLong("fees_cents"),
            if (r.isNull("cost_cents")) null else r.getLong("cost_cents"),
            if (r.has("buyer_paid_cents")) r.getLong("buyer_paid_cents") else null,
            if (r.has("discount_points_cents")) r.getLong("discount_points_cents") else null,
            if (r.has("ozon_subsidy_cents")) r.getLong("ozon_subsidy_cents") else null)
    }
}

internal data class ProfitState(val server: String = "", val from: String = LocalDate.now().minusDays(29).toString(),
    val to: String = LocalDate.now().toString(), val daysInput: String = "30", val report: String? = null,
    val loading: Boolean = false, val saving: Boolean = false, val error: String? = null,
    val cached: Boolean = false, val progress: String = "Получаем заказы и начисления…") {
    val valid: Boolean get() = runCatching {
        val a = LocalDate.parse(from); val b = LocalDate.parse(to)
        (daysInput.isEmpty() || daysInput.toIntOrNull() in 1..365) && !a.isAfter(b) && !b.isAfter(LocalDate.now()) && java.time.temporal.ChronoUnit.DAYS.between(a, b) < 365
    }.getOrDefault(false)
}

internal class ProfitabilityViewModel(app: Application) : AndroidViewModel(app) {
    private val mutable = MutableStateFlow(ProfitState())
    val state = mutable.asStateFlow()
    private fun cache(server: String): File {
        val key = MessageDigest.getInstance("SHA-256").digest(server.toByteArray(Charsets.UTF_8)).joinToString("") { "%02x".format(it) }
        return File(getApplication<Application>().filesDir, "profitability-$key.json")
    }
    fun connect(server: String) {
        if (server == mutable.value.server) return
        val saved = runCatching { cache(server).readText(Charsets.UTF_8).also { parseProfitRows(JSONObject(it)) } }.getOrNull()
        val json = saved?.let(::JSONObject)
        mutable.value = ProfitState(server = server, report = saved, cached = saved != null,
            from = json?.getString("date_from") ?: LocalDate.now().minusDays(29).toString(),
            to = json?.getString("date_to") ?: LocalDate.now().toString(),
            daysInput = if (json == null) "30" else "")
    }
    fun dates(from: String, to: String) { mutable.value = mutable.value.copy(from = from, to = to, daysInput = "") }
    fun days(value: String) {
        val count = value.toIntOrNull()
        val range = count?.takeIf { it in 1..365 }?.let { profitabilityRange(it) }
        mutable.value = mutable.value.copy(daysInput = value,
            from = range?.first?.toString() ?: mutable.value.from,
            to = range?.second?.toString() ?: mutable.value.to)
    }
    private fun persist(server: String, raw: String) {
        val target = cache(server); val temp = File(target.path + ".tmp")
        temp.writeText(raw, Charsets.UTF_8)
        java.nio.file.Files.move(temp.toPath(), target.toPath(), java.nio.file.StandardCopyOption.REPLACE_EXISTING)
    }
    fun refresh(fullReload: Boolean = false) {
        val selected = mutable.value
        val range = selected.daysInput.toIntOrNull()?.takeIf { it in 1..365 }?.let { profitabilityRange(it) }
        val before = if (range == null) selected else selected.copy(from = range.first.toString(), to = range.second.toString())
        if (!before.valid || before.loading || before.saving || before.server.isBlank()) return
        mutable.value = before.copy(loading = true, error = null)
        viewModelScope.launch {
            try {
                val raw = withContext(Dispatchers.IO) {
                    val started = JSONObject(serverRequest(before.server, "/api/v1/profitability/jobs?date_from=${before.from}&date_to=${before.to}&full_reload=$fullReload", body = JSONObject()))
                    val jobId = started.getString("job_id")
                    var completed: String? = null
                    repeat(600) {
                        if (completed == null) {
                            kotlinx.coroutines.delay(3000)
                            val status = JSONObject(serverRequest(before.server, "/api/v1/profitability/jobs/$jobId"))
                            withContext(Dispatchers.Main) {
                                if (mutable.value.server == before.server) mutable.value = mutable.value.copy(progress = status.optString("progress", "Загружаем отчёт…"))
                            }
                            when (status.getString("status")) {
                                "complete" -> completed = status.getJSONObject("report").toString()
                                "error" -> throw IllegalStateException(status.getString("error"))
                            }
                        }
                    }
                    (completed ?: throw IllegalStateException("Загрузка продолжается дольше ожидаемого. Повторите обновление позже.")).also {
                        parseProfitRows(JSONObject(it)); persist(before.server, it)
                    }
                }
                if (mutable.value.server == before.server) mutable.value = mutable.value.copy(report = raw, cached = false)
            } catch (e: Exception) {
                if (mutable.value.server == before.server) mutable.value = mutable.value.copy(error =
                    e.message ?: "Не удалось загрузить отчёт. Сохранённые данные доступны ниже.", cached = true)
            } finally {
                if (mutable.value.server == before.server) mutable.value = mutable.value.copy(loading = false)
            }
        }
    }
    fun saveCost(id: String, cents: Long, onSaved: () -> Unit) {
        val before = mutable.value
        if (before.saving || before.loading) return
        mutable.value = before.copy(saving = true, error = null)
        viewModelScope.launch {
            try {
                val raw = withContext(Dispatchers.IO) {
                    serverRequest(before.server, "/api/v1/profitability/cost", body = JSONObject().put("product_id", id).put("cost_cents", cents))
                    val report = JSONObject(requireNotNull(before.report))
                    val rows = report.getJSONArray("rows")
                    for (i in 0 until rows.length()) if (rows.getJSONObject(i).getString("product_id") == id) rows.getJSONObject(i).put("cost_cents", cents)
                    report.toString().also { persist(before.server, it) }
                }
                if (mutable.value.server == before.server) { mutable.value = mutable.value.copy(report = raw); onSaved() }
            } catch (e: Exception) {
                mutable.value = mutable.value.copy(error = (e as? ApiException)?.message ?: "Не удалось сохранить себестоимость на сервере. Изменение не применено.")
            } finally { mutable.value = mutable.value.copy(saving = false) }
        }
    }
}

internal fun totalProfit(rows: List<ProfitRow>, unboundExpenses: Long = 0L): Long =
    rows.sumOf { it.profit ?: 0L } - unboundExpenses

@Composable
internal fun ProfitabilityScreen(server: String, onBack: () -> Unit, model: ProfitabilityViewModel = viewModel()) {
    BackHandler(onBack = onBack)
    val state by model.state.collectAsStateWithLifecycle()
    LaunchedEffect(server) { model.connect(server) }
    var editId by rememberSaveable { mutableStateOf<String?>(null) }
    var costText by rememberSaveable { mutableStateOf("") }
    var query by rememberSaveable { mutableStateOf("") }
    val report = state.report?.let { JSONObject(it) }
    val rows = report?.let(::parseProfitRows).orEmpty()
    Scaffold(bottomBar = { Text("© ESI.Company · ${BuildConfig.VERSION_NAME}", Modifier.navigationBarsPadding().padding(12.dp)) }) { padding ->
        LazyColumn(Modifier.fillMaxSize().padding(padding), contentPadding = PaddingValues(20.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            item {
                TextButton(onClick = onBack) { Text("← План поставок") }
                Text("Рентабельность", style = MaterialTheme.typography.headlineMedium, fontWeight = FontWeight.Bold)
                Text("Заказы FBO + FBS — по дате создания. Начисления по ним — до дня загрузки. Общие расходы — за выбранный период.", style = MaterialTheme.typography.bodySmall)
                listOf(listOf("7", "14", "30"), listOf("60", "90")).forEach { periods ->
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        periods.forEach { days ->
                            OutlinedButton(onClick = { model.days(days) }, enabled = !state.loading && !state.saving) { Text("$days дней") }
                        }
                    }
                }
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    TextButton(onClick = {
                        val today = LocalDate.now(java.time.ZoneId.of("Europe/Moscow"))
                        model.dates(today.withDayOfMonth(1).toString(), today.toString())
                    }, enabled = !state.loading && !state.saving) { Text("Текущий месяц") }
                    TextButton(onClick = {
                        val last = LocalDate.now(java.time.ZoneId.of("Europe/Moscow")).withDayOfMonth(1).minusDays(1)
                        model.dates(last.withDayOfMonth(1).toString(), last.toString())
                    }, enabled = !state.loading && !state.saving) { Text("Прошлый месяц") }
                }
                OutlinedTextField(state.daysInput, model::days, label = { Text("Период, дней") },
                    supportingText = { Text("Отсчитывается назад от сегодня, включая сегодняшний день. Можно также задать даты вручную.") },
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number), singleLine = true,
                    enabled = !state.loading && !state.saving, modifier = Modifier.fillMaxWidth().padding(top = 8.dp),
                    isError = state.daysInput.isNotEmpty() && state.daysInput.toIntOrNull() !in 1..365)
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    OutlinedTextField(state.from, { model.dates(it, state.to) }, label = { Text("С даты") }, singleLine = true, modifier = Modifier.weight(1f), enabled = !state.loading)
                    OutlinedTextField(state.to, { model.dates(state.from, it) }, label = { Text("По дату") }, singleLine = true, modifier = Modifier.weight(1f), enabled = !state.loading)
                }
                if (!state.valid) Text("Выберите от 1 до 365 дней, не позднее сегодня", color = MaterialTheme.colorScheme.error)
                Button(onClick = { model.refresh() }, enabled = state.valid && !state.loading && !state.saving && server.isNotBlank(), modifier = Modifier.fillMaxWidth().padding(top = 8.dp)) {
                    Text(if (state.loading) "Получаем заказы и начисления…" else "Обновить")
                }
                OutlinedButton(onClick = { model.refresh(true) }, enabled = state.valid && !state.loading && !state.saving, modifier = Modifier.fillMaxWidth()) { Text("Полная сверка") }
                Text("Быстрое обновление проверяет последние 14 дней заново, старую историю использует до суток. Полная сверка получает все начисления заново.", style = MaterialTheme.typography.bodySmall)
                if (state.loading) {
                    LinearProgressIndicator(Modifier.fillMaxWidth())
                    Text(state.progress + ". Если Ozon ограничит запросы, сервер подождёт и продолжит загрузку.", style = MaterialTheme.typography.bodySmall)
                }
                state.error?.let { Text(it, color = MaterialTheme.colorScheme.error) }
                if (report != null) {
                    if (report.optString("accounting_basis") != "order_cash_v2") Text("Сохранён отчёт с другим принципом расчёта. Нажмите «Обновить».", color = MaterialTheme.colorScheme.error)
                    Text("Отчёт: ${report.getString("date_from")} — ${report.getString("date_to")}")
                    Text("Загружено: ${report.getString("fetched_at")}", style = MaterialTheme.typography.bodySmall)
                    if (state.cached || report.optBoolean("stale")) Text("Показан сохранённый отчёт. Свежесть данных не подтверждена.", color = MaterialTheme.colorScheme.error)
                    if (!report.isNull("warning")) Text(report.getString("warning"), style = MaterialTheme.typography.bodySmall)
                    Text("В карточках учитываются только удержания с точной связью с заказом и SKU. Исключено из карточек строк: ${report.optInt("excluded_accruals")}. Налоги и внешние расходы не включены.", style = MaterialTheme.typography.bodySmall)
                    Text("Выручка включает оплату покупателей и компенсации Ozon. Начисления после конца периода тоже учитываются, если относятся к выбранным заказам.", style = MaterialTheme.typography.bodySmall)
                    Card(Modifier.fillMaxWidth().padding(vertical = 8.dp)) {
                        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(5.dp)) {
                            Text("Заработано за период", style = MaterialTheme.typography.titleMedium)
                            Text(currency(totalProfit(rows, if (report.optBoolean("expenses_complete")) report.getLong("unbound_expenses_cents") else 0L)),
                                style = MaterialTheme.typography.headlineMedium, fontWeight = FontWeight.Bold)
                            if (report.optBoolean("expenses_complete")) {
                                Text("Прибыль товаров: ${currency(totalProfit(rows))}")
                                Text("Общие расходы Ozon за период: ${currency(report.getLong("unbound_expenses_cents"))}", fontWeight = FontWeight.SemiBold)
                                val expenses = report.getJSONArray("unbound_expenses")
                                for (index in 0 until expenses.length()) {
                                    val expense = expenses.getJSONObject(index)
                                    Text("${expense.getString("name")}: ${currency(expense.getLong("amount_cents"))}", style = MaterialTheme.typography.bodySmall)
                                }
                                Text("Общие расходы вычтены полностью, включая товары без себестоимости. Период расходов — по дате начисления; возвраты расходов уменьшают сумму.", style = MaterialTheme.typography.bodySmall)
                            } else {
                                Text("Общие расходы ещё не загружены и не вычтены. Нажмите «Обновить».", color = MaterialTheme.colorScheme.error)
                            }
                            Text("Прибыль только по товарам с указанной себестоимостью. Учтено: ${rows.count { it.cost != null }} из ${rows.size}. Без себестоимости: ${rows.count { it.cost == null }}. Налоги и внешние расходы не включены.",
                                style = MaterialTheme.typography.bodySmall)
                        }
                    }
                    OutlinedTextField(query, { query = it }, label = { Text("Название или артикул") }, modifier = Modifier.fillMaxWidth())
                } else if (!state.loading) Text("Выберите период и нажмите «Обновить». Себестоимость общая для всех пользователей.")
            }
            items(rows.filter { it.name.contains(query, true) || it.offer.contains(query, true) }, key = { it.id }) { row ->
                Card(Modifier.fillMaxWidth(), shape = RoundedCornerShape(16.dp)) {
                    Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(5.dp)) {
                        Text(row.name, fontWeight = FontWeight.Bold)
                        Text(row.offer, style = MaterialTheme.typography.bodySmall)
                        Text("Заказано: ${formatQuantity(row.ordered.toLong())} · Выкуплено: ${formatQuantity(row.redeemed.toLong())} · Возвращено: ${formatQuantity(row.returned.toLong())}")
                        Text("Выручка с учётом возвратов: ${currency(row.revenue)}")
                        row.buyerPaid?.let { Text("Оплата покупателей с возвратами: ${currency(it)}") }

                        row.subsidy?.let { Text("Доплаты Ozon с возвратами: ${currency(it)}") }
                        Text("Удержания Ozon: ${currency(row.fees)}")
                        Text("Средние удержания на заказанную единицу: ${row.averageFee?.let(::currency) ?: "—"}")
                        TextButton(onClick = { editId = row.id; costText = row.cost?.let { BigDecimal.valueOf(it, 2).toPlainString() } ?: "" }, enabled = !state.saving && !state.loading) {
                            Text("Себестоимость: ${row.cost?.let(::currency) ?: "не задана"} · Изменить")
                        }
                        Text("Порог стоимости: ${row.costThreshold?.let(::currency) ?: "—"}", fontWeight = FontWeight.SemiBold)
                        Text("Прибыль: ${row.profit?.let(::currency) ?: "задайте себестоимость"}", fontWeight = FontWeight.Bold)
                        Text("Маржинальность: ${row.margin ?: "—"}")
                    }
                }
            }
        }
    }
    val editRow = rows.find { it.id == editId }
    if (editRow != null) AlertDialog(onDismissRequest = { if (!state.saving) editId = null },
        title = { Text("Себестоимость за единицу") }, text = {
            Column {
                Text(editRow.name)
                Text("Общее значение для всех пользователей и всех периодов. Применяется последняя сохранённая себестоимость.")
                OutlinedTextField(costText, { costText = it }, label = { Text("Рубли") }, keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal), enabled = !state.saving, isError = costCents(costText) == null)
                state.error?.let { Text(it, color = MaterialTheme.colorScheme.error) }
            }
        }, confirmButton = {
            TextButton(onClick = { costCents(costText)?.let { model.saveCost(editRow.id, it) { editId = null } } }, enabled = costCents(costText) != null && !state.saving) { Text(if (state.saving) "Сохраняем…" else "Сохранить для всех") }
        }, dismissButton = { TextButton(onClick = { editId = null }, enabled = !state.saving) { Text("Отмена") } })
}
