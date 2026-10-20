from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


@dataclass(frozen=True)
class ServerSettings:
    client_id: str = field(repr=False)
    api_key: str = field(repr=False)
    timezone: str = "Europe/Moscow"
    cache_seconds: int = 300
    retry_seconds: int = 30
    snapshot_path: Path = Path("data/forecast.json")
    update_apk_path: Path = Path("updates/ozon-plan.apk")

    @classmethod
    def from_env(cls) -> ServerSettings:
        load_dotenv(os.getenv("FORECAST_ENV_FILE", ".env"), encoding="utf-8")
        client = os.getenv("OZON_CLIENT_ID", "").strip()
        key = os.getenv("OZON_API_KEY", "").strip()
        if not client or not key or key.startswith("replace_"):
            raise RuntimeError("Заполните OZON_CLIENT_ID и OZON_API_KEY в .env на сервере")
        timezone = os.getenv("REPORT_TIMEZONE", "Europe/Moscow")
        ZoneInfo(timezone)
        return cls(
            client_id=client,
            api_key=key,
            timezone=timezone,
            snapshot_path=Path(os.getenv("FORECAST_SNAPSHOT_PATH", "data/forecast.json")),
            update_apk_path=Path(os.getenv("FORECAST_UPDATE_APK", "updates/ozon-plan.apk")),
        )
