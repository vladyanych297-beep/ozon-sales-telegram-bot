from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv


@dataclass(frozen=True, slots=True)
class Settings:
    telegram_bot_token: str
    ozon_client_id: str
    ozon_api_key: str
    database_path: Path
    ozon_api_base_url: str
    report_timezone: ZoneInfo
    allowed_telegram_group_id: int

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        required = {
            "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            "OZON_CLIENT_ID": os.getenv("OZON_CLIENT_ID", "").strip(),
            "OZON_API_KEY": os.getenv("OZON_API_KEY", "").strip(),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise RuntimeError(f"Не заданы обязательные переменные: {', '.join(missing)}")

        raw_group_id = os.getenv("ALLOWED_TELEGRAM_GROUP_ID", "").strip()
        if not raw_group_id:
            raise RuntimeError("Не задана обязательная переменная: ALLOWED_TELEGRAM_GROUP_ID")
        try:
            allowed_group_id = int(raw_group_id)
        except ValueError as exc:
            raise RuntimeError("ALLOWED_TELEGRAM_GROUP_ID должен быть целым числом") from exc

        timezone_name = os.getenv("REPORT_TIMEZONE", "Europe/Moscow")
        try:
            timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise RuntimeError(f"Неизвестный часовой пояс REPORT_TIMEZONE: {timezone_name}") from exc

        return cls(
            telegram_bot_token=required["TELEGRAM_BOT_TOKEN"],
            ozon_client_id=required["OZON_CLIENT_ID"],
            ozon_api_key=required["OZON_API_KEY"],
            database_path=Path(os.getenv("DATABASE_PATH", "data/bot.sqlite3")),
            ozon_api_base_url=os.getenv(
                "OZON_API_BASE_URL", "https://api-seller.ozon.ru"
            ).rstrip("/"),
            report_timezone=timezone,
            allowed_telegram_group_id=allowed_group_id,
        )
