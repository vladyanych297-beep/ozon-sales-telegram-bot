from __future__ import annotations

import json
import sqlite3
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
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    def get(self, user_id: int) -> UserPreferences:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT period_days, selected_skus_json FROM user_preferences "
                "WHERE telegram_user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            return UserPreferences()
        selected = None if row[1] is None else frozenset(json.loads(row[1]))
        return UserPreferences(period_days=int(row[0]), selected_skus=selected)

    def set_period(self, user_id: int, days: int) -> None:
        if days not in {7, 14, 30, 60, 90}:
            raise ValueError("Недопустимый период")
        self._ensure_user(user_id)
        with self._connect() as connection:
            connection.execute(
                "UPDATE user_preferences SET period_days = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE telegram_user_id = ?",
                (days, user_id),
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
        self._upsert(user_id, current.period_days, frozenset(selected))
        return self.get(user_id)

    def _ensure_user(self, user_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO user_preferences (telegram_user_id) VALUES (?)",
                (user_id,),
            )

    def _upsert(
        self, user_id: int, period_days: int, selected_skus: frozenset[str] | None
    ) -> None:
        payload = None if selected_skus is None else json.dumps(sorted(selected_skus))
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO user_preferences
                    (telegram_user_id, period_days, selected_skus_json)
                VALUES (?, ?, ?)
                ON CONFLICT(telegram_user_id) DO UPDATE SET
                    period_days = excluded.period_days,
                    selected_skus_json = excluded.selected_skus_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (user_id, period_days, payload),
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

