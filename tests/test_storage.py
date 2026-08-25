from pathlib import Path
from datetime import date

from ozon_sales_bot.storage import PreferencesStorage


def test_preferences_defaults_and_persistence(tmp_path: Path):
    storage = PreferencesStorage(tmp_path / "bot.sqlite3")
    storage.initialize()

    assert storage.get(42).period_days == 30
    assert storage.get(42).all_products

    storage.set_period(42, 14)
    storage.toggle_sku(42, "sku-1")
    saved = storage.get(42)

    assert saved.period_days == 14
    assert saved.selected_skus == frozenset({"sku-1"})

    storage.set_custom_period(42, date(2026, 8, 1), date(2026, 8, 25))
    saved = storage.get(42)
    assert saved.custom_period
    assert saved.date_from == date(2026, 8, 1)
    assert saved.date_to == date(2026, 8, 25)

    storage.set_period(42, 30)
    saved = storage.get(42)
    assert not saved.custom_period
    assert saved.period_days == 30

    storage.set_all_products(42)
    assert storage.get(42).all_products


def test_existing_database_is_migrated_for_custom_dates(tmp_path: Path):
    database = tmp_path / "bot.sqlite3"
    import sqlite3

    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE user_preferences (
                telegram_user_id INTEGER PRIMARY KEY,
                period_days INTEGER NOT NULL DEFAULT 30,
                selected_skus_json TEXT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

    storage = PreferencesStorage(database)
    storage.initialize()
    storage.set_custom_period(7, date(2026, 7, 1), date(2026, 7, 31))

    assert storage.get(7).date_from == date(2026, 7, 1)


def test_update_offset(tmp_path: Path):
    storage = PreferencesStorage(tmp_path / "bot.sqlite3")
    storage.initialize()

    assert storage.get_update_offset() is None

    storage.set_update_offset(123)
    assert storage.get_update_offset() == 123

    storage.set_update_offset(456)
    assert storage.get_update_offset() == 456

    assert storage.get_state("menu_keyboard_version") is None
    storage.set_state("menu_keyboard_version", "1")
    assert storage.get_state("menu_keyboard_version") == "1"
