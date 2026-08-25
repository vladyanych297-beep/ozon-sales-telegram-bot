from __future__ import annotations

from datetime import date
from html import escape

from .models import SaleRow


MAX_MESSAGE_LENGTH = 3900


def format_report(rows: list[SaleRow], date_from: date, date_to: date) -> list[str]:
    header = (
        "<b>Заказы Ozon</b>\n"
        f"Период: {date_from:%d.%m.%Y}–{date_to:%d.%m.%Y}\n\n"
    )
    if not rows:
        return [header + "За выбранный период продаж по выбранным товарам нет."]

    total_units = sum(row.ordered_units for row in rows)
    summary = (
        f"Номенклатур: <b>{len(rows)}</b>\n"
        f"Заказано: <b>{total_units} шт.</b>\n"
        "Отменённые отправления исключены.\n\n"
    )
    blocks = [
        f"<b>{index}. {escape(row.name)}</b>\n"
        f"SKU: <code>{escape(row.sku)}</code>\n"
        f"Заказано: {row.ordered_units} шт.\n"
        for index, row in enumerate(rows, start=1)
    ]

    messages: list[str] = []
    current = header + summary
    for block in blocks:
        if len(current) + len(block) + 1 > MAX_MESSAGE_LENGTH:
            messages.append(current.rstrip())
            current = "<b>Продолжение отчёта</b>\n\n"
        current += block + "\n"
    messages.append(current.rstrip())
    return messages
