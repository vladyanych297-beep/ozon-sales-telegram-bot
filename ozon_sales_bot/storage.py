from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

from .models import UserPreferences


class PreferencesStorage:
    def __init__(self, path: Path) -> None:
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS user_preferences (
                    telegram_user_id INTEGER PRIMARY KEY,
                    period_days INTEGER NOT NULL DEFAULT 30,
                    selected_skus_json TEXT NULL,
                    date_from TEXT NULL,
                    date_to TEXT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(user_preferences)")
            }
            if "date_from" not in columns:
                connection.execute(
                    "ALTER TABLE user_preferences ADD COLUMN date_from TEXT NULL"
                )
            if "date_to" not in columns:
                connection.execute(
                    "ALTER TABLE user_preferences ADD COLUMN date_to TEXT NULL"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )

    def get_update_offset(self) -> int | None:
        value = self.get_state("telegram_update_offset")
        return None if value is None else int(value)

    def set_update_offset(self, offset: int) -> None:
        self.set_state("telegram_update_offset", str(offset))

    def get_state(self, key: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM bot_state WHERE key = ?", (key,)
            ).fetchone()
        return None if row is None else str(row[0])

    def set_state(self, key: str, value: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO bot_state (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def get(self, user_id: int) -> UserPreferences:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT period_days, selected_skus_json, date_from, date_to "
                "FROM user_preferences "
                "WHERE telegram_user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            return UserPreferences()
        selected = None if row[1] is None else frozenset(json.loads(row[1]))
        date_from = None if row[2] is None else date.fromisoformat(row[2])
        date_to = None if row[3] is None else date.fromisoformat(row[3])
        return UserPreferences(
            period_days=int(row[0]),
            selected_skus=selected,
            date_from=date_from,
            date_to=date_to,
        )

    def set_period(self, user_id: int, days: int) -> None:
        if days not in {7, 14, 30, 60, 90}:
            raise ValueError("Недопустимый период")
        self._ensure_user(user_id)
        with self._connect() as connection:
            connection.execute(
                "UPDATE user_preferences SET period_days = ?, date_from = NULL, "
                "date_to = NULL, updated_at = CURRENT_TIMESTAMP "
                "WHERE telegram_user_id = ?",
                (days, user_id),
            )

    def set_custom_period(self, user_id: int, date_from: date, date_to: date) -> None:
        if date_from > date_to:
            raise ValueError("Дата начала не может быть позже даты окончания")
        self._ensure_user(user_id)
        with self._connect() as connection:
            connection.execute(
                "UPDATE user_preferences SET date_from = ?, date_to = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE telegram_user_id = ?",
                (date_from.isoformat(), date_to.isoformat(), user_id),
            )

    def set_all_products(self, user_id: int) -> None:
        self._ensure_user(user_id)
        with self._connect() as connection:
            connection.execute(
                "UPDATE user_preferences SET selected_skus_json = NULL, "
                "updated_at = CURRENT_TIMESTAMP WHERE telegram_user_id = ?",
                (user_id,),
            )

    def toggle_sku(self, user_id: int, sku: str) -> UserPreferences:
        current = self.get(user_id)
        selected = set() if current.selected_skus is None else set(current.selected_skus)
        if sku in selected:
            selected.remove(sku)
        else:
            selected.add(sku)
        self._upsert(
            user_id,
            current.period_days,
            frozenset(selected),
            current.date_from,
            current.date_to,
        )
        return self.get(user_id)

    def _ensure_user(self, user_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO user_preferences (telegram_user_id) VALUES (?)",
                (user_id,),
            )

    def _upsert(
        self,
        user_id: int,
        period_days: int,
        selected_skus: frozenset[str] | None,
        date_from: date | None,
        date_to: date | None,
    ) -> None:
        payload = None if selected_skus is None else json.dumps(sorted(selected_skus))
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO user_preferences
                    (telegram_user_id, period_days, selected_skus_json, date_from, date_to)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(telegram_user_id) DO UPDATE SET
                    period_days = excluded.period_days,
                    selected_skus_json = excluded.selected_skus_json,
                    date_from = excluded.date_from,
                    date_to = excluded.date_to,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    user_id,
                    period_days,
                    payload,
                    None if date_from is None else date_from.isoformat(),
                    None if date_to is None else date_to.isoformat(),
                ),
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)
