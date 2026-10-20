package ru.ozonplan.app

import org.json.JSONObject
import java.net.URI
import kotlin.math.roundToLong

data class ProductRow(
    val id: String,
    val offerId: String,
    val name: String,
    val skus: List<String>,
    val sales: Long,
    val stock: Long,
    val inTransit: Long,
    val clusters: List<ClusterStock> = emptyList(),
) {
    fun toSupply(days: Int, salesDays: Int): Long =
        calculateSupply(sales, days, stock, inTransit, salesDays)
}

enum class ClusterMetric(val label: String) {
    SALES("Продажи"), STOCK("Остаток"), IN_TRANSIT("В пути"),
    TO_SUPPLY("К поставке"), STOCK_DAYS("Запас текущего остатка"),
}

internal fun selectedClusterMetrics(saved: Set<String>?): Set<ClusterMetric> =
    if (saved == null) ClusterMetric.entries.toSet()
    else ClusterMetric.entries.filter { it.name in saved }.toSet()

data class ClusterStock(
    val name: String, val scheme: String, val sales: Long,
    val stock: Long, val inTransit: Long,
) {
    fun toSupply(days: Int, salesDays: Int): Long =
        calculateSupply(sales, days, stock, inTransit, salesDays)

    fun stockDays(salesDays: Int): Double? = calculateStockDays(sales, stock, salesDays)
}

internal fun formatStockDays(days: Double?): String? =
    days?.let { "${it.roundToLong()} дн." }

fun calculateStockDays(sales: Long, stock: Long, salesDays: Int): Double? {
    require(sales >= 0 && stock >= 0 && salesDays in 1..365)
    return if (sales == 0L) null else stock.toDouble() * salesDays / sales
}

fun calculateSupply(
    sales: Long, days: Int, stock: Long, inTransit: Long, salesDays: Int = 30,
): Long {
    require(days in 1..365 && salesDays in 1..365 && sales >= 0 && stock >= 0 && inTransit >= 0)
    val demand = Math.multiplyExact(sales, days.toLong())
    // Round the positive demand first; subtracting integer stocks is mathematically equivalent.
    val rounded = demand / salesDays + if (demand % salesDays != 0L) 1 else 0
    return (rounded - minOf(rounded, stock) - minOf(rounded - minOf(rounded, stock), inTransit))
}

data class Report(
    val rows: List<ProductRow>, val fetchedAt: String,
    val dateFrom: String, val dateTo: String, val timezone: String,
    val stale: Boolean, val warning: String?,
    val salesPeriodDays: Int, val forecastPeriodDays: Int,
)

data class WarehouseStock(
    val name: String, val scheme: String, val stock: Long, val inTransit: Long,
)

data class WarehouseProduct(
    val id: String, val offerId: String, val name: String,
    val skus: List<String>, val warehouses: List<WarehouseStock>,
)

data class WarehouseReport(
    val rows: List<WarehouseProduct>, val fetchedAt: String, val timezone: String,
)

fun parseReport(text: String): Report {
    val root = JSONObject(text)
    require(root.getInt("schema_version") == 1) { "Неподдерживаемая версия сервера" }
    val source = root.getJSONArray("rows")
    val seen = mutableSetOf<String>()
    val rows = List(source.length()) { index ->
        val r = source.getJSONObject(index)
        val id = r.getString("product_id")
        require(seen.add(id)) { "Сервер вернул повторяющиеся товары" }
        fun quantity(key: String): Long {
            val raw = r.get(key)
            require(raw is Int || raw is Long) { "Некорректное количество" }
            return (raw as Number).toLong().also { require(it in 0..1_000_000_000_000L) }
        }
        val skus = r.getJSONArray("skus")
        val clusterSource = r.optJSONArray("clusters")
        val clusters = if (clusterSource == null) emptyList() else List(clusterSource.length()) { clusterIndex ->
            val cluster = clusterSource.getJSONObject(clusterIndex)
            val scheme = cluster.getString("scheme")
            require(scheme == "FBO" || scheme == "FBS")
            fun amount(key: String): Long {
                val raw = cluster.get(key)
                require(raw is Int || raw is Long)
                return (raw as Number).toLong().also { require(it in 0..1_000_000_000_000L) }
            }
            ClusterStock(cluster.getString("name"), scheme, amount("sales"),
                amount("stock"), amount("in_transit"))
        }.sortedWith(compareByDescending<ClusterStock> { it.sales }
            .thenByDescending { it.stock }
            .thenByDescending { it.inTransit }
            .thenBy { it.name.lowercase() }
            .thenBy { it.scheme })
        ProductRow(id, r.getString("offer_id"), r.getString("name"),
            List(skus.length()) { skus.getString(it) },
            quantity("sales_30_days"), quantity("stock"), quantity("in_transit"), clusters)
    }
    return Report(rows, root.getString("fetched_at"), root.getString("sales_date_from"),
        root.getString("sales_date_to"), root.getString("timezone"), root.getBoolean("stale"),
        if (root.isNull("warning")) null else root.getString("warning"),
        root.optInt("sales_period_days", 30).also { require(it in 1..365) },
        root.getInt("period_days").also { require(it in 1..365) })
}

fun encodeReportCache(server: String, reportText: String): String {
    parseReport(reportText)
    return JSONObject()
        .put("cache_version", 1)
        .put("server", server)
        .put("report", JSONObject(reportText))
        .toString()
}

fun decodeReportCache(text: String, expectedServer: String): Report? {
    val root = JSONObject(text)
    if (root.getInt("cache_version") != 1 || root.getString("server") != expectedServer) {
        return null
    }
    return parseReport(root.getJSONObject("report").toString())
}

fun parseWarehouseReport(text: String): WarehouseReport {
    val root = JSONObject(text)
    require(root.getInt("schema_version") == 1) { "Неподдерживаемая версия сервера" }
    val source = root.getJSONArray("rows")
    val seenProducts = mutableSetOf<String>()
    val rows = List(source.length()) { index ->
        val item = source.getJSONObject(index)
        val id = item.getString("product_id")
        require(seenProducts.add(id)) { "Сервер вернул повторяющиеся товары" }
        val skuSource = item.getJSONArray("skus")
        val warehouseSource = item.getJSONArray("warehouses")
        val seenWarehouses = mutableSetOf<String>()
        val warehouses = List(warehouseSource.length()) { warehouseIndex ->
            val warehouse = warehouseSource.getJSONObject(warehouseIndex)
            val scheme = warehouse.getString("scheme")
            require(scheme == "FBO" || scheme == "FBS") { "Неизвестная схема склада" }
            val name = warehouse.getString("warehouse_name")
            require(name.isNotBlank() && seenWarehouses.add("$scheme\u0000$name")) {
                "Некорректный или повторяющийся склад"
            }
            fun quantity(key: String): Long {
                val raw = warehouse.get(key)
                require(raw is Int || raw is Long) { "Некорректное количество" }
                return (raw as Number).toLong().also { require(it in 0..1_000_000_000_000L) }
            }
            WarehouseStock(name, scheme, quantity("stock"), quantity("in_transit"))
        }
        require(warehouses.isNotEmpty()) { "Товар без складских остатков" }
        WarehouseProduct(
            id, item.getString("offer_id"), item.getString("name"),
            List(skuSource.length()) { skuSource.getString(it) }, warehouses,
        )
    }
    return WarehouseReport(rows, root.getString("fetched_at"), root.getString("timezone"))
}

fun normalizeServer(value: String, allowLocalHttp: Boolean): String {
    val uri = try { URI(value.trim()) } catch (_: Exception) { throw IllegalArgumentException("Некорректный адрес сервера") }
    require(uri.host != null && uri.userInfo == null && uri.query == null && uri.fragment == null) {
        "Введите адрес сервера без логина, параметров и фрагмента"
    }
    require(uri.path.isNullOrEmpty() || uri.path == "/") { "Укажите только адрес сервера без пути" }
    val local = uri.host in setOf("localhost", "127.0.0.1", "10.0.2.2")
    require(uri.scheme == "https" || (allowLocalHttp && local && uri.scheme == "http")) {
        "Для подключения требуется HTTPS"
    }
    return value.trim().trimEnd('/')
}
