from datetime import date

from ozon_sales_bot.keyboards import (
    MENU_BUTTON_TEXT,
    calendar_menu,
    main_menu,
    period_menu,
    persistent_menu_keyboard,
)
from ozon_sales_bot.models import UserPreferences


def test_persistent_menu_keyboard():
    keyboard = persistent_menu_keyboard()

    assert keyboard.is_persistent
    assert keyboard.resize_keyboard
    assert keyboard.keyboard[0][0].text == MENU_BUTTON_TEXT


def test_main_menu_contains_report_and_filters():
    menu = main_menu(UserPreferences())
    assert menu.inline_keyboard[0][0].callback_data == "report"
    assert menu.inline_keyboard[1][0].callback_data == "period:menu"
    assert "30 дней" in menu.inline_keyboard[1][0].text
    assert menu.inline_keyboard[1][1].callback_data == "products:0"


def test_main_menu_shows_custom_period():
    menu = main_menu(
        UserPreferences(date_from=date(2026, 8, 1), date_to=date(2026, 8, 25))
    )

    assert "01.08.2026–25.08.2026" in menu.inline_keyboard[1][0].text


def test_period_menu_contains_calendar_button():
    menu = period_menu(UserPreferences())

    assert any(
        button.callback_data == "period:calendar"
        for row in menu.inline_keyboard
        for button in row
    )


def test_calendar_disables_future_dates():
    menu = calendar_menu(2026, 8, "start", date(2026, 8, 25))
    buttons = [button for row in menu.inline_keyboard for button in row]

    assert any(button.callback_data == "cal:day:start:2026-08-25" for button in buttons)
    assert not any(button.callback_data == "cal:day:start:2026-08-26" for button in buttons)
