package ru.ozonplan.app

import com.sun.net.httpserver.HttpServer
import org.junit.Assert.*
import org.junit.Test
import java.net.InetSocketAddress
import java.nio.file.Files
import java.security.MessageDigest
import java.util.concurrent.atomic.AtomicInteger

class ForecastTest {
    @Test fun stockDaysDisplayRoundsToNearestWholeDay() {
        assertEquals("6 дн.", formatStockDays(6.4))
        assertEquals("7 дн.", formatStockDays(6.5))
        assertEquals("7 дн.", formatStockDays(6.9))
        assertEquals("0 дн.", formatStockDays(0.0))
        assertNull(formatStockDays(null))
        assertEquals(6.5, calculateStockDays(2, 13, 1)!!, 0.0)
    }

    @Test fun clusterDisplaySelectionHidesOnlyRequestedMetrics() {
        val selected = setOf(ClusterMetric.STOCK, ClusterMetric.TO_SUPPLY)
        val text = clusterMetricsText(5, 7, 9, selected)
        assertEquals("Остаток: 7", text.text)
        assertEquals("7", text.text.substring(text.spanStyles.single().start,
            text.spanStyles.single().end))
        assertEquals("", clusterMetricsText(5, 7, 9, emptySet()).text)
        assertEquals(ClusterMetric.entries.toSet(), selectedClusterMetrics(null))
        assertEquals(emptySet<ClusterMetric>(), selectedClusterMetrics(emptySet()))
        assertEquals(selected, selectedClusterMetrics(selected.map { it.name }.toSet()))
        assertEquals(setOf(ClusterMetric.SALES), selectedClusterMetrics(setOf("SALES", "UNKNOWN")))
    }

    @Test fun clusterSupplyUsesOwnStockTransitAndSelectedPeriods() {
        val cluster = ClusterStock("Москва", "FBO", 7, 3, 2)
        assertEquals(9L, cluster.toSupply(30, 15))
        assertEquals(0L, cluster.toSupply(1, 15))
        assertEquals(45.0 / 7, cluster.stockDays(15)!!, 0.00001)
        assertEquals(0.0, calculateStockDays(3, 0, 30)!!, 0.0)
        assertNull(calculateStockDays(0, 12, 30))
        assertNull(calculateStockDays(0, 0, 30))
        assertThrows(IllegalArgumentException::class.java) { calculateStockDays(1, 1, 0) }
        assertTrue(calculateStockDays(1, Long.MAX_VALUE, 365)!!.isFinite())
    }

    @Test fun clusterMetricsBoldOnlyNonzeroValues() {
        val metrics = clusterMetricsText(5, 0, 7)
        assertEquals(setOf("5", "7"), metrics.spanStyles.map {
            metrics.text.substring(it.start, it.end)
        }.toSet())
        assertTrue(metrics.text.contains("Остаток: 0"))
    }

    @Test fun updateDownloadsVerifiedRangesAndRetriesInterruptedPart() {
        val content = ByteArray(1024 * 1024 + 23) { (it % 251).toByte() }
        val hash = MessageDigest.getInstance("SHA-256").digest(content)
            .joinToString("") { "%02x".format(it) }
        val attempts = AtomicInteger()
        val server = HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0)
        server.createContext("/api/v1/update/apk") { exchange ->
            try {
                assertNull(exchange.requestHeaders.getFirst("Authorization"))
                if (attempts.incrementAndGet() == 1) {
                    exchange.sendResponseHeaders(503, -1)
                } else {
                    val range = requireNotNull(Regex("bytes=([0-9]+)-([0-9]+)")
                        .matchEntire(exchange.requestHeaders.getFirst("Range")))
                    val start = range.groupValues[1].toInt()
                    val end = range.groupValues[2].toInt()
                    val part = content.copyOfRange(start, end + 1)
                    exchange.responseHeaders.add("Content-Range", "bytes $start-$end/${content.size}")
                    exchange.sendResponseHeaders(206, part.size.toLong())
                    exchange.responseBody.write(part)
                }
            } finally { exchange.close() }
        }
        val directory = Files.createTempDirectory("ozon-update-test").toFile()
        try {
            server.start()
            val info = AvailableUpdate(19, "0.5.8", content.size.toLong(), hash)
            val progress = mutableListOf<UpdateProgress>()
            val result = downloadAvailableUpdate(
                "http://127.0.0.1:${server.address.port}", info, directory, true, progress::add
            )
            assertArrayEquals(content, result.readBytes())
            assertEquals(4, attempts.get())
            assertTrue(progress.any { it.stage == UpdateStage.DOWNLOADING && it.attempt == 2 })
            assertTrue(progress.any { it.stage == UpdateStage.DOWNLOADING && it.percent in 1..99 })
            assertEquals(listOf(UpdateStage.VERIFYING, UpdateStage.SAVING, UpdateStage.READY),
                progress.takeLast(3).map { it.stage })
            assertEquals(content.size.toLong(), progress.last().bytes)
            assertEquals(100, progress.last().percent)
            progress.clear()
            assertEquals(result, downloadAvailableUpdate(
                "http://127.0.0.1:${server.address.port}", info, directory, true, progress::add))
            assertEquals(listOf(UpdateStage.VERIFYING, UpdateStage.READY), progress.map { it.stage })
            assertEquals(4, attempts.get())
            progress.clear()
            assertThrows(IllegalArgumentException::class.java) {
                downloadAvailableUpdate("http://127.0.0.1:${server.address.port}",
                    info.copy(sha256 = "0".repeat(64)), directory, true, progress::add)
            }
            assertFalse(progress.any { it.stage == UpdateStage.READY })
        } finally {
            server.stop(0)
            directory.deleteRecursively()
        }
    }
    @Test fun updateMetadataRejectsInvalidAndOversizedPackages() {
        val digest = "a".repeat(64)
        val valid = """{"version_code":14,"version_name":"0.5.3","size_bytes":123,"sha256":"$digest"}"""
        assertEquals(14, parseAvailableUpdate(valid).code)
        assertEquals(123L, parseAvailableUpdate(valid).size)
        assertThrows(IllegalArgumentException::class.java) {
            parseAvailableUpdate(valid.replace("123", "100000001"))
        }
        assertThrows(IllegalArgumentException::class.java) {
            parseAvailableUpdate(valid.replace(digest, "bad"))
        }
    }
    @Test fun roundsUpAndNeverReturnsNegative() {
        assertEquals(15L, calculateSupply(30, 30, 5, 10))
        assertEquals(1L, calculateSupply(1, 1, 0, 0))
        assertEquals(0L, calculateSupply(1, 29, 1, 0))
        assertEquals(0L, calculateSupply(0, 30, 1, 5))
        assertEquals(60L, calculateSupply(100, 45, 70, 20))
        assertEquals(7L, calculateSupply(7, 30, 0, 0))
        assertEquals(0L, calculateSupply(1, 1, Long.MAX_VALUE, Long.MAX_VALUE))
        assertEquals(35L, calculateSupply(30, 20, 3, 2, 15))
    }
    @Test fun serverMustUseHttpsWithoutCredentials() {
        assertEquals("https://example.com", normalizeServer(" https://example.com/ ", false))
        assertEquals("http://10.0.2.2:8000", normalizeServer("http://10.0.2.2:8000", true))
        for (value in listOf("http://example.com", "https://a:b@example.com", "https://example.com/path", "ftp://example.com", "https://example.com?key=secret")) {
            assertThrows(IllegalArgumentException::class.java) { normalizeServer(value, false) }
        }
    }
    @Test fun malformedOrPartialReportIsRejected() {
        assertThrows(Exception::class.java) { parseReport("{}") }
        assertThrows(Exception::class.java) { parseReport("{\"schema_version\":2}") }
    }
    @Test fun cachedReportIsBoundToItsServerAndValidated() {
        val raw = """{
          "schema_version":1,"period_days":60,"sales_period_days":30,
          "fetched_at":"2026-09-30T12:00:00+03:00",
          "sales_date_from":"2026-09-01","sales_date_to":"2026-09-30",
          "timezone":"Europe/Moscow","stale":false,"warning":null,"rows":[]
        }"""
        val cached = encodeReportCache("https://one.example", raw)
        assertEquals(60, decodeReportCache(cached, "https://one.example")?.forecastPeriodDays)
        assertNull(decodeReportCache(cached, "https://two.example"))
        assertThrows(Exception::class.java) {
            decodeReportCache(cached.replace("\"schema_version\":1", "\"schema_version\":2"),
                "https://one.example")
        }
    }
    @Test fun clustersAreSortedBySalesStockAndTransitDescending() {
        val report = parseReport("""{
          "schema_version":1,"period_days":60,"sales_period_days":30,
          "fetched_at":"2026-09-30T12:00:00+03:00",
          "sales_date_from":"2026-09-01","sales_date_to":"2026-09-30",
          "timezone":"Europe/Moscow","stale":false,"warning":null,
          "rows":[{"product_id":"1","offer_id":"A","name":"Test","skus":["10"],
          "sales_30_days":20,"stock":12,"in_transit":11,"clusters":[
            {"name":"В","scheme":"FBO","sales":5,"stock":3,"in_transit":8},
            {"name":"Б","scheme":"FBO","sales":5,"stock":4,"in_transit":1},
            {"name":"Г","scheme":"FBO","sales":6,"stock":0,"in_transit":0},
            {"name":"А","scheme":"FBO","sales":5,"stock":4,"in_transit":2}
          ]}]}
        """)
        assertEquals(listOf("Г", "А", "Б", "В"),
            report.rows.single().clusters.map { it.name })
    }
    @Test fun readsReportAndRecalculatesNewPeriod() {
        val report = parseReport("""{
          "schema_version":1,"period_days":60,"sales_period_days":15,
          "fetched_at":"2026-08-27T12:00:00+03:00",
          "sales_date_from":"2026-07-29","sales_date_to":"2026-08-27",
          "timezone":"Europe/Moscow","stale":false,"warning":null,
          "rows":[{"product_id":"1","offer_id":"A","name":"Test","skus":["10"],
          "sales_30_days":30,"stock":2,"in_transit":3}]}""")
        assertEquals(115L, report.rows.single().toSupply(
            report.forecastPeriodDays, report.salesPeriodDays))
        assertEquals(15, report.salesPeriodDays)
        assertEquals(60, report.forecastPeriodDays)
        assertFalse(report.stale)
    }
    @Test fun readsWarehouseHierarchy() {
        val report = parseWarehouseReport("""{
          "schema_version":1,"fetched_at":"2026-09-22T12:00:00+03:00",
          "timezone":"Europe/Moscow","rows":[{
            "product_id":"1","offer_id":"A","name":"Test","skus":["10"],
            "warehouses":[
              {"warehouse_name":"FBO Москва","scheme":"FBO","stock":7,"in_transit":3},
              {"warehouse_name":"Склад продавца","scheme":"FBS","stock":4,"in_transit":0}
            ]
          }]}
        """)
        assertEquals("Test", report.rows.single().name)
        assertEquals(7L, report.rows.single().warehouses.first().stock)
        assertEquals(3L, report.rows.single().warehouses.first().inTransit)
        assertEquals("FBS", report.rows.single().warehouses.last().scheme)
    }
}
