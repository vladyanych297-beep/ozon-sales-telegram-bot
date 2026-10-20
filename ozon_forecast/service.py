from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from ozon_sales_bot.models import SaleRow
from ozon_sales_bot.ozon import OzonApiError

from .client import ForecastOzonClient, OzonRateLimitError
from .models import (
    ClusterRow,
    ForecastReport,
    ForecastRow,
    Inventory,
    Product,
    WarehouseInventory,
    WarehouseProductRow,
    WarehouseStockReport,
    WarehouseStockRow,
    units_to_supply,
)
from .settings import ServerSettings

logger = logging.getLogger(__name__)


def combine_rows(
    products: list[Product],
    sales: list[SaleRow],
    inventory: dict[str, Inventory],
    days: int,
    sales_days: int = 30,
    warehouse_inventory: list[WarehouseInventory] | None = None,
) -> list[ForecastRow]:
    sku_to_id: dict[str, str] = {}
    if len({p.product_id for p in products}) != len(products):
        raise OzonApiError("В каталоге повторяется product_id")
    for product in products:
        for sku in product.skus:
            if sku in sku_to_id and sku_to_id[sku] != product.product_id:
                raise OzonApiError("SKU принадлежит нескольким товарам; расчёт остановлен")
            sku_to_id[sku] = product.product_id
    totals: dict[str, int] = {}
    clusters: dict[str, dict[tuple[str, str], list[int]]] = {}
    for sale in sales:
        if sale.sku not in sku_to_id:
            raise OzonApiError(
                "В продажах есть SKU, отсутствующий в каталоге; требуется сверка товаров"
            )
        product_id = sku_to_id[sale.sku]
        if sale.ordered_units < 0:
            raise OzonApiError("Ozon вернул отрицательное количество заказов")
        totals[product_id] = totals.get(product_id, 0) + sale.ordered_units
        cluster_key = ("FBO", sale.cluster or "Кластер не определён")
        clusters.setdefault(product_id, {}).setdefault(cluster_key, [0, 0, 0])[0] += sale.ordered_units
    for item in warehouse_inventory or []:
        if item.scheme != "FBO":
            continue
        product_id = sku_to_id.get(item.sku)
        if product_id is None:
            raise OzonApiError("В остатках по кластерам есть неизвестный SKU")
        key = (item.scheme, item.warehouse_name)
        values = clusters.setdefault(product_id, {}).setdefault(key, [0, 0, 0])
        values[1] += item.stock
        values[2] += item.in_transit
    rows = []
    for product in products:
        sold = totals.get(product.product_id, 0)
        stock = sum(inventory.get(s, Inventory()).available for s in product.skus)
        transit = sum(inventory.get(s, Inventory()).in_transit for s in product.skus)
        rows.append(
            ForecastRow(
                product_id=product.product_id,
                offer_id=product.offer_id,
                name=product.name,
                skus=sorted(product.skus),
                sales_30_days=sold,
                stock=stock,
                in_transit=transit,
                forecast_units=units_to_supply(sold, days, 0, 0, sales_days),
                to_supply=units_to_supply(sold, days, stock, transit, sales_days),
                clusters=[
                    ClusterRow(name=name, scheme=scheme, sales=values[0],
                               stock=values[1], in_transit=values[2])
                    for (scheme, name), values in sorted(
                        clusters.get(product.product_id, {}).items(),
                        key=lambda item: (
                            -item[1][0],
                            -item[1][1],
                            -item[1][2],
                            item[0][1].casefold(),
                            item[0][0],
                        ),
                    )
                    if any(values)
                ],
            )
        )
    return sorted(rows, key=lambda r: (-r.to_supply, -r.sales_30_days, r.name.casefold()))


def combine_warehouse_rows(
    products: list[Product], inventory: list[WarehouseInventory]
) -> list[WarehouseProductRow]:
    sku_to_product: dict[str, Product] = {}
    for product in products:
        for sku in product.skus:
            previous = sku_to_product.get(sku)
            if previous is not None and previous.product_id != product.product_id:
                raise OzonApiError("SKU принадлежит нескольким товарам; остатки не показаны")
            sku_to_product[sku] = product
    grouped: dict[str, dict[tuple[str, str], list[int]]] = {}
    for item in inventory:
        if item.scheme != "FBO":
            continue
        product = sku_to_product.get(item.sku)
        if product is None:
            raise OzonApiError("В остатках по складам есть неизвестный SKU")
        key = (item.warehouse_name, item.scheme)
        totals = grouped.setdefault(product.product_id, {}).setdefault(key, [0, 0])
        totals[0] += item.stock
        totals[1] += item.in_transit
    result = []
    for product in products:
        warehouses = [
            WarehouseStockRow(
                warehouse_name=name, scheme=scheme, stock=values[0], in_transit=values[1]
            )
            for (name, scheme), values in grouped.get(product.product_id, {}).items()
            if values[0] or values[1]
        ]
        warehouses.sort(key=lambda row: (row.scheme, row.warehouse_name.casefold()))
        if warehouses:
            result.append(WarehouseProductRow(
                product_id=product.product_id,
                offer_id=product.offer_id,
                name=product.name,
                skus=sorted(product.skus),
                warehouses=warehouses,
            ))
    return sorted(result, key=lambda row: (row.name.casefold(), row.offer_id.casefold()))


@dataclass(frozen=True)
class Snapshot:
    rows: list[ForecastRow]
    fetched_at: datetime
    date_from: date
    date_to: date


class ForecastService:
    def __init__(self, settings: ServerSettings, client: ForecastOzonClient) -> None:
        self.settings = settings
        self.client = client
        self._lock = asyncio.Lock()
        self._snapshot: Snapshot | None = None
        self._loaded_at = 0.0
        self._retry_at = 0.0
        self._last_error: str | None = None
        self._rate_limited = False
        self._warehouse_lock = asyncio.Lock()
        self._warehouse_report: WarehouseStockReport | None = None
        self._warehouse_loaded_at = 0.0
        try:
            saved = json.loads(settings.snapshot_path.read_text(encoding="utf-8"))
            if (
                not isinstance(saved, dict)
                or saved.get("cluster_sales_direction") != "to"
                or saved.get("cluster_inventory_scope") != "fbo_only"
                or saved.get("cluster_sales_scheme") != "fbo_combined"
            ):
                raise ValueError("Устаревшая разбивка продаж по кластерам")
            report = ForecastReport.model_validate(saved)
            if report.timezone == settings.timezone:
                self._snapshot = Snapshot(
                    report.rows,
                    report.fetched_at,
                    report.sales_date_from,
                    report.sales_date_to,
                )
        except (OSError, ValueError):
            pass

    def _fresh(self, today: date, sales_days: int) -> bool:
        return (
            self._snapshot is not None
            and self._snapshot.date_to == today
            and self._snapshot.date_from == today - timedelta(days=sales_days - 1)
            and self._loaded_at > 0
            and time.monotonic() - self._loaded_at < self.settings.cache_seconds
        )

    async def report(self, days: int, sales_days: int = 30) -> ForecastReport:
        if not 1 <= days <= 365 or not 1 <= sales_days <= 365:
            raise ValueError("Периоды от 1 до 365 дней")
        today = datetime.now(ZoneInfo(self.settings.timezone)).date()
        if not self._fresh(today, sales_days):
            async with self._lock:
                if not self._fresh(today, sales_days) and time.monotonic() >= self._retry_at:
                    try:
                        date_from = today - timedelta(days=sales_days - 1)
                        async with asyncio.timeout(180):
                            products = await self.client.get_catalog()
                            sales, inventory = await asyncio.gather(
                                self.client.get_sales(date_from, today),
                                self.client.get_inventory(products),
                            )
                            warehouse_inventory = await self.client.get_warehouse_inventory(products)
                        rows = combine_rows(products, sales, inventory, days, sales_days,
                                            warehouse_inventory)
                        self._snapshot = Snapshot(
                            rows,
                            datetime.now(ZoneInfo(self.settings.timezone)),
                            date_from,
                            today,
                        )
                        self._loaded_at = time.monotonic()
                        self._last_error = None
                        self._rate_limited = False
                        self._save()
                    except (OzonApiError, TimeoutError) as exc:
                        self._rate_limited = isinstance(exc, OzonRateLimitError)
                        delay = max(self.settings.retry_seconds, getattr(exc, "retry_seconds", 0))
                        self._retry_at = time.monotonic() + delay
                        self._last_error = (
                            str(OzonRateLimitError(delay))
                            if self._rate_limited
                            else str(exc) or "Обновление Ozon заняло слишком много времени"
                        )
        if self._snapshot is None:
            if self._rate_limited:
                raise OzonRateLimitError(max(1, math.ceil(self._retry_at - time.monotonic())))
            raise OzonApiError(self._last_error or "Данные ещё не загружены")
        snapshot = self._snapshot
        actual_sales_days = (snapshot.date_to - snapshot.date_from).days + 1
        rows = [
            r.model_copy(
                update={
                    "forecast_units": units_to_supply(
                        r.sales_30_days, days, 0, 0, actual_sales_days
                    ),
                    "to_supply": units_to_supply(
                        r.sales_30_days, days, r.stock, r.in_transit, actual_sales_days
                    ),
                }
            )
            for r in snapshot.rows
        ]
        rows.sort(key=lambda r: (-r.to_supply, -r.sales_30_days, r.name.casefold()))
        return ForecastReport(
            period_days=days,
            sales_period_days=actual_sales_days,
            sales_date_from=snapshot.date_from,
            sales_date_to=snapshot.date_to,
            timezone=self.settings.timezone,
            fetched_at=snapshot.fetched_at,
            stale=not self._fresh(today, sales_days),
            warning=self._last_error,
            rows=rows,
        )

    async def warehouse_stocks(self) -> WarehouseStockReport:
        if (
            self._warehouse_report is not None
            and time.monotonic() - self._warehouse_loaded_at < self.settings.cache_seconds
        ):
            return self._warehouse_report
        async with self._warehouse_lock:
            if (
                self._warehouse_report is not None
                and time.monotonic() - self._warehouse_loaded_at < self.settings.cache_seconds
            ):
                return self._warehouse_report
            async with asyncio.timeout(180):
                if self._snapshot is not None:
                    products = [
                        Product(row.product_id, row.offer_id, row.name, frozenset(row.skus))
                        for row in self._snapshot.rows
                    ]
                else:
                    products = await self.client.get_catalog()
                inventory = await self.client.get_warehouse_inventory(products)
            report = WarehouseStockReport(
                timezone=self.settings.timezone,
                fetched_at=datetime.now(ZoneInfo(self.settings.timezone)),
                rows=combine_warehouse_rows(products, inventory),
            )
            self._warehouse_report = report
            self._warehouse_loaded_at = time.monotonic()
            return report

    def _save(self) -> None:
        snapshot = self._snapshot
        if snapshot is None:
            return
        report = ForecastReport(
            period_days=30,
            sales_period_days=(snapshot.date_to - snapshot.date_from).days + 1,
            sales_date_from=snapshot.date_from,
            sales_date_to=snapshot.date_to,
            timezone=self.settings.timezone,
            fetched_at=snapshot.fetched_at,
            rows=snapshot.rows,
        )
        path = self.settings.snapshot_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps({
                    **report.model_dump(mode="json"),
                    "cluster_sales_direction": "to",
                    "cluster_inventory_scope": "fbo_only",
                    "cluster_sales_scheme": "fbo_combined",
                }, ensure_ascii=False),
                encoding="utf-8"
            )
            temporary.replace(path)
        except OSError:
            logger.warning("Could not save forecast snapshot")
