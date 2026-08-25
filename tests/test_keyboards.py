from ozon_sales_bot.keyboards import (
    MENU_BUTTON_TEXT,
    SELECTOR_URL,
    main_menu,
    persistent_menu_keyboard,
    selector_link_keyboard,
)
from ozon_sales_bot.models import UserPreferences


def test_persistent_menu_keyboard():
    keyboard = persistent_menu_keyboard()

    assert keyboard.is_persistent
    assert keyboard.resize_keyboard
    assert keyboard.keyboard[0][0].text == MENU_BUTTON_TEXT


def test_selector_url_is_available_in_both_menus():
    assert selector_link_keyboard().inline_keyboard[0][0].url == SELECTOR_URL
    menu = main_menu(UserPreferences())
    assert menu.inline_keyboard[0][0].url == SELECTOR_URL
    assert menu.inline_keyboard[1][0].callback_data == "report"
    assert menu.inline_keyboard[2][0].callback_data == "period:menu"
    assert "30 дней" in menu.inline_keyboard[2][0].text
    assert menu.inline_keyboard[2][1].callback_data == "products:0"
