package ru.ozonplan.app

import org.junit.Assert.*
import org.junit.Test

class ProfitabilityTest {
    @Test fun daysPeriodIncludesTodayAndHandlesMonthBoundaries() {
        val today = java.time.LocalDate.of(2026, 3, 1)
        assertEquals(today to today, profitabilityRange(1, today))
        assertEquals(java.time.LocalDate.of(2026, 1, 31) to today, profitabilityRange(30, today))
        assertEquals(365L, java.time.temporal.ChronoUnit.DAYS.between(profitabilityRange(365, today).first, today) + 1)
        assertFalse(ProfitState(daysInput = "0").valid)
    }
    @Test fun totalProfitIncludesLossesAndRequiresAllCosts() {
        val row = ProfitRow("1", "offer", "name", 2, 2, 1, 50000, 12000, 20000)
        assertEquals(18000L, totalProfit(listOf(row)))
        assertEquals(-14000L, totalProfit(listOf(row, row.copy(cost = 70000))))
        assertNull(totalProfit(listOf(row, row.copy(cost = null))))
        assertEquals(0L, totalProfit(emptyList()))
    }
    @Test fun costInputIsExactAndNonnegative() {
        assertEquals(12345L, costCents("123,45"))
        assertEquals(0L, costCents("0"))
        assertNull(costCents("-1"))
        assertNull(costCents("1.001"))
        assertNull(costCents("NaN"))
    }
    @Test fun returnedGoodsReverseRevenueAndCost() {
        val row = ProfitRow("1", "offer", "name", 2, 2, 1, 50000, 12000, 20000)
        assertEquals(18000L, row.profit)
        assertEquals("36.00 %", row.margin)
        assertEquals(6000L, row.averageFee)
        assertEquals(26000L, row.costThreshold)
        assertEquals(36000L, row.copy(cost = 30000).costThreshold)
        assertEquals(6000L, row.copy(cost = 0).costThreshold)
        assertNull(row.copy(cost = null).costThreshold)
        assertNull(row.copy(ordered = 0).costThreshold)
        assertNull(row.copy(cost = null).profit)
        assertNull(row.copy(revenue = 0).margin)
        assertNull(row.copy(ordered = 0).averageFee)
    }
}
