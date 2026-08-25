from ozon_sales_bot.keyboards import (
    MENU_BUTTON_TEXT,
    main_menu,
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
