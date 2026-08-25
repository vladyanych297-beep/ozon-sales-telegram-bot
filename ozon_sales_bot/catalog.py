from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

from .config import Settings
from .ozon import OzonClient


async def export_catalog(output_path: Path) -> None:
    settings = Settings.from_env()
    client = OzonClient(
        settings.ozon_api_base_url,
        settings.ozon_client_id,
        settings.ozon_api_key,
    )
    try:
        today = datetime.now(settings.report_timezone).date()
        products = await client.get_sales(today - timedelta(days=89), today)
    finally:
        await client.close()

    payload = {
        "updated_at": datetime.now(settings.report_timezone).isoformat(timespec="seconds"),
        "products": [
            {"sku": row.sku, "name": row.name}
            for row in sorted(products, key=lambda item: item.name.casefold())
        ],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("docs/products.json"))
    args = parser.parse_args()
    asyncio.run(export_catalog(args.output))


if __name__ == "__main__":
    main()
