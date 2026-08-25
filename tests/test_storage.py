from pathlib import Path

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

    storage.set_all_products(42)
    assert storage.get(42).all_products


def test_update_offset(tmp_path: Path):
    storage = PreferencesStorage(tmp_path / "bot.sqlite3")
    storage.initialize()

    assert storage.get_update_offset() is None

    storage.set_update_offset(123)
    assert storage.get_update_offset() == 123

    storage.set_update_offset(456)
    assert storage.get_update_offset() == 456
