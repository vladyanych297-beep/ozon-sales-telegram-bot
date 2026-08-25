from datetime import date

from ozon_sales_bot.formatting import format_report
from ozon_sales_bot.models import SaleRow


def test_report_contains_totals_and_escapes_html():
    messages = format_report(
        [SaleRow(sku="123", name="Товар <тест>", ordered_units=2)],
        date(2026, 7, 27),
        date(2026, 8, 25),
    )

    assert len(messages) == 1
    assert "2 шт." in messages[0]
    assert "₽" not in messages[0]
    assert "Отменённые отправления исключены" in messages[0]
    assert "Товар &lt;тест&gt;" in messages[0]


def test_empty_report():
    messages = format_report([], date(2026, 8, 1), date(2026, 8, 25))
    assert "продаж" in messages[0]
