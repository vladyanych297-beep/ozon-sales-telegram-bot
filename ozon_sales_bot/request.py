from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass


REQUEST_PATTERN = re.compile(r"#ozon_sales\s+([A-Za-z0-9_-]+)")
ALLOWED_PERIODS = {7, 14, 30, 60, 90}
MAX_SELECTED_SKUS = 500


@dataclass(frozen=True, slots=True)
class SalesRequest:
    period_days: int
    selected_skus: frozenset[str] | None


def parse_sales_request(text: str) -> SalesRequest:
    match = REQUEST_PATTERN.search(text)
    if match is None:
        raise ValueError("Запрос не найден")

    encoded = match.group(1)
    padding = "=" * (-len(encoded) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(encoded + padding))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Некорректный формат запроса") from exc

    if not isinstance(payload, dict) or payload.get("p") not in ALLOWED_PERIODS:
        raise ValueError("Некорректный период запроса")

    raw_skus = payload.get("s")
    if raw_skus == "all":
        selected_skus = None
    elif isinstance(raw_skus, list) and 0 < len(raw_skus) <= MAX_SELECTED_SKUS:
        normalized = [str(sku).strip() for sku in raw_skus]
        if any(not sku or len(sku) > 100 for sku in normalized):
            raise ValueError("Некорректный список номенклатур")
        selected_skus = frozenset(normalized)
    else:
        raise ValueError("Некорректный список номенклатур")

    return SalesRequest(period_days=int(payload["p"]), selected_skus=selected_skus)
