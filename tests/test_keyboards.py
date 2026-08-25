from ozon_sales_bot.keyboards import MENU_BUTTON_TEXT, persistent_menu_keyboard


def test_persistent_menu_keyboard():
    keyboard = persistent_menu_keyboard()

    assert keyboard.is_persistent
    assert keyboard.resize_keyboard
    assert keyboard.keyboard[0][0].text == MENU_BUTTON_TEXT
