from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .models import SaleRow, UserPreferences


PERIODS = (7, 14, 30, 60, 90)
PAGE_SIZE = 8
MENU_BUTTON_TEXT = "📋 Меню"


def persistent_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=MENU_BUTTON_TEXT)]],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Нажмите «Меню» для выбора отчёта",
    )


def main_menu(preferences: UserPreferences) -> InlineKeyboardMarkup:
    products_label = "Все" if preferences.all_products else str(len(preferences.selected_skus or ()))
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📊 Получить заказы",
                    callback_data="report",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"📅 Период: {preferences.period_days} дней",
                    callback_data="period:menu",
                ),
                InlineKeyboardButton(
                    text=f"📦 Товары: {products_label}",
                    callback_data="products:0",
                ),
            ],
        ]
    )


def period_menu(current_days: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for days in PERIODS:
        marker = "✅ " if days == current_days else ""
        builder.button(text=f"{marker}{days} дней", callback_data=f"period:set:{days}")
    builder.adjust(2, 2, 1)
    builder.row(InlineKeyboardButton(text="← Назад", callback_data="menu"))
    return builder.as_markup()


def products_menu(
    products: list[SaleRow], preferences: UserPreferences, page: int
) -> InlineKeyboardMarkup:
    page_count = max(1, (len(products) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(page, 0), page_count - 1)
    start = page * PAGE_SIZE
    selected = preferences.selected_skus
    builder = InlineKeyboardBuilder()
    all_marker = "✅ " if selected is None else ""
    builder.row(
        InlineKeyboardButton(text=f"{all_marker}Все товары", callback_data="products:all")
    )
    for row in products[start : start + PAGE_SIZE]:
        marker = "✅ " if selected is not None and row.sku in selected else "▫️ "
        label = row.name if len(row.name) <= 42 else row.name[:39] + "…"
        builder.row(
            InlineKeyboardButton(
                text=f"{marker}{label}", callback_data=f"product:toggle:{row.sku}:{page}"
            )
        )
    navigation: list[InlineKeyboardButton] = []
    if page > 0:
        navigation.append(InlineKeyboardButton(text="←", callback_data=f"products:{page - 1}"))
    navigation.append(InlineKeyboardButton(text=f"{page + 1}/{page_count}", callback_data="noop"))
    if page + 1 < page_count:
        navigation.append(InlineKeyboardButton(text="→", callback_data=f"products:{page + 1}"))
    builder.row(*navigation)
    builder.row(InlineKeyboardButton(text="Готово", callback_data="menu"))
    return builder.as_markup()
