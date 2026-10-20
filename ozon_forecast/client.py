from __future__ import annotations

import asyncio
import logging
import math
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from email.utils import parsedate_to_datetime
from time import monotonic
from typing import Any
from zoneinfo import ZoneInfo

from ozon_sales_bot.models import SaleRow
from ozon_sales_bot.ozon import OzonApiError, OzonClient

from .models import Inventory, Product, WarehouseInventory

logger = logging.getLogger(__name__)

FBO_TRANSIT_STATES = frozenset(
    {
        "READY_TO_SUPPLY",
        "ACCEPTED_AT_SUPPLY_WAREHOUSE",
        "IN_TRANSIT",
        "ACCEPTANCE_AT_STORAGE_WAREHOUSE",
    }
)


def retry_after_seconds(value: str | None) -> int:
    if value:
        try:
            seconds = int(value) if value.isdecimal() else math.ceil(
                (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
            )
            if 0 < seconds < 10**9:
                return seconds
        except (ValueError, TypeError, OverflowError):
            pass
    return 60


class OzonRateLimitError(OzonApiError):
    def __init__(self, retry_seconds: int) -> None:
        self.retry_seconds = retry_seconds
        super().__init__(
            f"Ozon ограничил частоту запросов (429). "
            f"Повторите обновление не раньше чем через {retry_seconds} сек."
        )


def nonnegative_int(value: Any, field: str) -> int:
    # Missing quantities must not turn into a plausible but incorrect zero.
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise OzonApiError(f"Ozon: некорректное поле {field}")
    try:
        result = int(value)
    except ValueError as exc:
        raise OzonApiError(f"Ozon: некорректное поле {field}") from exc
    if result < 0:
        raise OzonApiError(f"Ozon: отрицательное поле {field}")
    return result


class ForecastOzonClient(OzonClient):
    """Only list/read requests; no stock changes or supply creation."""

    FINANCIAL_DATA = True

    def __init__(self, client_id: str, api_key: str, report_timezone: str) -> None:
        super().__init__("https://api-seller.ozon.ru", client_id, api_key)
        self.report_timezone = ZoneInfo(report_timezone)
        # Ozon applies both a shared Client-Id limit and method-specific limits.
        # Serialize calls and spread them out so one manual refresh does not
        # create a short burst from the nested asyncio.gather() calls.
        self._request_lock = asyncio.Lock()
        self._last_request_at = 0.0
        self._min_request_interval = 0.25
        # A bundle id identifies an immutable revision of a supply composition.
        # Reuse it while the supply remains active instead of downloading the
        # same products again on every refresh.
        self._bundle_cache: dict[str, dict[str, int]] = {}
        self._cluster_cache: dict[str, tuple[float, dict[str, str]]] = {}

    async def get_sales(
        self, date_from: date, date_to: date,
        selected_skus: frozenset[str] | None = None,
    ) -> list[SaleRow]:
        """Aggregate non-cancelled, non-returned units by destination cluster."""
        fbo, fbs, returned = await asyncio.gather(
            self._get_fbo_postings(date_from, date_to),
            self._get_fbs_postings(date_from, date_to),
            self._get_returned_quantities(date_from, date_to),
        )
        totals: dict[tuple[str, str], tuple[str, int]] = {}
        for postings in (fbo, fbs):
            for posting in postings:
                status = str(posting.get("status") or "").lower()
                alias = str(posting.get("status_alias") or "").lower()
                if "cancel" in status or "cancel" in alias:
                    continue
                financial = posting.get("financial_data") or {}
                if not isinstance(financial, dict):
                    raise OzonApiError("Ozon вернул некорректные финансовые данные отправления")
                cluster = str(financial.get("cluster_to") or "").strip() or "Кластер назначения не определён"
                posting_number = str(posting.get("posting_number") or "").strip()
                for product in posting.get("products") or []:
                    sku = str(product.get("sku") or product.get("product_id") or "").strip()
                    if not sku or (selected_skus is not None and sku not in selected_skus):
                        continue
                    name = str(product.get("name") or product.get("product_name") or sku).strip()
                    quantity = nonnegative_int(product.get("quantity"), "quantity")
                    quantity = max(0, quantity - returned.get((posting_number, sku), 0))
                    if not quantity:
                        continue
                    # FBS sales describe demand in the same destination cluster as FBO
                    # sales. Present both as one FBO demand figure; FBS stock is not
                    # relevant to replenishing Ozon fulfilment centres.
                    key = (sku, cluster)
                    previous_name, previous_quantity = totals.get(key, (name, 0))
                    totals[key] = (previous_name, previous_quantity + quantity)
        return [SaleRow(sku, name, quantity, cluster, "FBO")
                for (sku, cluster), (name, quantity) in sorted(totals.items())]

    async def _get_returned_quantities(
        self, date_from: date, date_to: date
    ) -> dict[tuple[str, str], int]:
        """Return quantities grouped by original posting and SKU.

        The returns endpoint includes both FBO and FBS. Matching by the original
        posting number prevents a return for an older order from reducing sales
        in the selected history period.
        """
        since, until = self._date_bounds(date_from, date_to)
        totals: dict[tuple[str, str], int] = defaultdict(int)
        last_id = 0
        seen_ids: set[str] = set()
        for _ in range(1000):
            data = await self._post(
                "/v1/returns/list",
                {
                    "filter": {
                        "logistic_return_date": {
                            "time_from": since,
                            "time_to": until,
                        }
                    },
                    "limit": 500,
                    "last_id": last_id,
                },
            )
            page = data.get("returns")
            has_next = data.get("has_next")
            if not isinstance(page, list) or not isinstance(has_next, bool):
                raise OzonApiError("Ozon вернул неизвестный формат списка возвратов")
            next_id = last_id
            for item in page:
                if not isinstance(item, dict):
                    raise OzonApiError("Ozon вернул некорректную строку возврата")
                return_id = str(item.get("id") or "").strip()
                if not return_id or return_id in seen_ids:
                    raise OzonApiError("Ozon вернул повторный или пустой идентификатор возврата")
                seen_ids.add(return_id)
                try:
                    next_id = max(next_id, int(return_id))
                except ValueError as exc:
                    raise OzonApiError("Ozon вернул некорректный идентификатор возврата") from exc
                posting_number = str(item.get("posting_number") or "").strip()
                product = item.get("product")
                if not posting_number or not isinstance(product, dict):
                    raise OzonApiError("В возврате отсутствуют отправление или товар")
                sku = str(product.get("sku") or "").strip()
                if not sku:
                    raise OzonApiError("В возврате отсутствует SKU")
                quantity = nonnegative_int(product.get("quantity"), "return quantity")
                totals[(posting_number, sku)] += quantity
            if not has_next:
                return dict(totals)
            if not page or next_id <= last_id:
                raise OzonApiError("Ozon не вернул следующую страницу возвратов")
            last_id = next_id
        raise OzonApiError("Слишком много страниц возвратов Ozon")

    async def _post(self, endpoint: str, body: dict[str, Any]) -> dict[str, Any]:
        import httpx

        # Do not expose Ozon error bodies, credentials, or internal URLs to APK users.
        for attempt in range(3):
            try:
                async with self._request_lock:
                    delay = self._min_request_interval - (monotonic() - self._last_request_at)
                    if delay > 0:
                        await asyncio.sleep(delay)
                    response = await self._http.post(endpoint, json=body)
                    self._last_request_at = monotonic()
            except httpx.HTTPError as exc:
                if attempt == 2:
                    raise OzonApiError("Не удалось подключиться к Ozon") from exc
            else:
                if response.status_code >= 400:
                    logger.warning("Ozon request failed: endpoint=%s status=%s",
                                   endpoint, response.status_code)
                if response.status_code == 429:
                    # Return immediately; further manual refreshes share the cooldown.
                    raise OzonRateLimitError(retry_after_seconds(response.headers.get("Retry-After")))
                if response.status_code in (401, 403):
                    raise OzonApiError(
                        f"Ключ Ozon не имеет доступа к {endpoint}. "
                        "Администратору нужно проверить срок действия и права ключа."
                    )
                if response.status_code != 429 and response.status_code < 500:
                    if response.is_error:
                        raise OzonApiError(
                            f"Ozon отклонил запрос {endpoint} ({response.status_code})"
                        )
                    try:
                        result = response.json()
                    except ValueError as exc:
                        raise OzonApiError("Ozon вернул некорректный JSON") from exc
                    if not isinstance(result, dict):
                        raise OzonApiError("Неизвестный формат ответа Ozon")
                    return result
            if attempt < 2:
                await asyncio.sleep(2**attempt)
        raise OzonApiError("Ozon временно недоступен (ошибка 5xx). Повторите обновление позже.")

    def _date_bounds(self, date_from: date, date_to: date) -> tuple[str, str]:
        start = datetime.combine(date_from, time.min, self.report_timezone)
        end = datetime.combine(date_to + timedelta(days=1), time.min, self.report_timezone)
        end -= timedelta(milliseconds=1)
        return tuple(
            dt.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            for dt in (start, end)
        )

    @staticmethod
    def _aggregate_postings(
        postings: list[dict], selected_skus: frozenset[str] | None
    ) -> list[SaleRow]:
        totals: dict[str, tuple[str, int]] = {}
        for posting in postings:
            if not isinstance(posting, dict):
                raise OzonApiError("Неизвестный формат отправления")
            if any(
                "cancel" in str(posting.get(field, "")).lower()
                for field in ("status", "status_alias")
            ):
                continue
            products = posting.get("products")
            if not isinstance(products, list):
                raise OzonApiError("В отправлении отсутствует список товаров")
            for product in products:
                if not isinstance(product, dict):
                    raise OzonApiError("Неизвестный формат товара в отправлении")
                sku = str(product.get("sku") or product.get("product_id") or "")
                if not sku:
                    raise OzonApiError("В отправлении отсутствует SKU")
                if selected_skus is not None and sku not in selected_skus:
                    continue
                quantity = nonnegative_int(product.get("quantity"), "quantity")
                name = str(product.get("name") or product.get("product_name") or sku)
                old = totals.get(sku, (name, 0))
                totals[sku] = (old[0], old[1] + quantity)
        return [SaleRow(sku, name, quantity) for sku, (name, quantity) in totals.items()]

    async def get_catalog(self) -> list[Product]:
        ids: set[str] = set()
        cursor = ""
        seen: set[str] = set()
        for _ in range(1000):
            data = await self._post(
                "/v3/product/list",
                {
                    "filter": {"visibility": "ALL"},
                    "last_id": cursor,
                    "limit": 1000,
                },
            )
            result = data.get("result", {})
            if not isinstance(result, dict):
                raise OzonApiError("Неизвестный формат каталога Ozon")
            items = result.get("items")
            if not isinstance(items, list):
                raise OzonApiError("Неизвестный формат каталога Ozon")
            for item in items:
                if not isinstance(item, dict) or not item.get("product_id"):
                    raise OzonApiError("В каталоге отсутствует product_id")
                ids.add(str(item["product_id"]))
            total = nonnegative_int(result.get("total"), "total")
            if len(ids) >= total:
                break
            if not items:
                raise OzonApiError("Ozon вернул пустую страницу до конца каталога")
            cursor = str(result.get("last_id") or "")
            if not cursor or cursor in seen:
                raise OzonApiError("Ozon не вернул следующую страницу каталога")
            seen.add(cursor)
        else:
            raise OzonApiError("Слишком большой каталог Ozon")

        products = []
        ordered_ids = sorted(ids)
        for start in range(0, len(ordered_ids), 1000):
            chunk = ordered_ids[start : start + 1000]
            data = await self._post("/v3/product/info/list", {"product_id": chunk})
            items = data.get("items")
            if not isinstance(items, list) or len(items) != len(chunk):
                raise OzonApiError("Ozon вернул неполную информацию о товарах")
            if any(not isinstance(item, dict) for item in items):
                raise OzonApiError("Неизвестный формат карточек товаров")
            if {str(item.get("id")) for item in items} != set(chunk):
                raise OzonApiError("Ozon вернул карточки других товаров")
            for item in items:
                sources = item.get("sources", [])
                if not isinstance(sources, list) or any(not isinstance(s, dict) for s in sources):
                    raise OzonApiError("Неизвестный формат sources в карточке товара")
                if not item.get("name") or not item.get("offer_id"):
                    raise OzonApiError("В карточке товара отсутствуют название или артикул")
                skus = {str(s["sku"]) for s in sources if s.get("sku")}
                if item.get("sku"):
                    skus.add(str(item["sku"]))
                products.append(
                    Product(
                        product_id=str(item["id"]),
                        offer_id=str(item["offer_id"]),
                        name=str(item["name"]),
                        skus=frozenset(skus),
                    )
                )
        return products

    async def get_inventory(self, products: list[Product]) -> dict[str, Inventory]:
        skus = {sku for product in products for sku in product.skus}
        product_ids = {product.product_id for product in products}
        available, transit = await asyncio.gather(
            self._get_available_inventory(product_ids, skus),
            self._get_fbo_transit(skus),
        )
        return {
            sku: Inventory(
                available.get(sku, 0),
                transit.get(sku, 0),
            )
            for sku in skus
            if sku in available or sku in transit
        }

    async def get_warehouse_inventory(
        self, products: list[Product]
    ) -> list[WarehouseInventory]:
        """Return non-empty balances with FBO warehouses mapped to Ozon clusters."""
        skus = {sku for product in products for sku in product.skus}
        fbo = await self._get_fbo_warehouse_inventory(skus)
        if fbo:
            clusters = await self._get_cluster_warehouse_map("CLUSTER_TYPE_OZON")
            if any(row.warehouse_name not in clusters for row in fbo):
                cis_clusters = await self._get_cluster_warehouse_map("CLUSTER_TYPE_CIS")
                for warehouse, name in cis_clusters.items():
                    if warehouse in clusters and clusters[warehouse] != name:
                        raise OzonApiError("Ozon отнёс склад к нескольким кластерам")
                    clusters[warehouse] = name
            missing = {row.warehouse_name for row in fbo if row.warehouse_name not in clusters}
            if missing:
                raise OzonApiError(
                    "Ozon не указал кластер для склада FBO: " + ", ".join(sorted(missing))
                )
            fbo = [
                WarehouseInventory(row.sku, clusters[row.warehouse_name], row.scheme,
                                   row.stock, row.in_transit)
                for row in fbo
            ]
        return fbo

    async def _get_cluster_warehouse_map(self, kind: str) -> dict[str, str]:
        cached = self._cluster_cache.get(kind)
        if cached and monotonic() < cached[0]:
            return cached[1].copy()
        data = await self._post("/v1/cluster/list", {"cluster_type": kind})
        clusters = data.get("clusters")
        if not isinstance(clusters, list):
            raise OzonApiError("Неизвестный формат кластеров Ozon")
        mapping: dict[str, str] = {}
        for cluster in clusters:
            if not isinstance(cluster, dict):
                raise OzonApiError("Неизвестный формат кластера Ozon")
            name = str(cluster.get("name") or "").strip()
            logistics = cluster.get("logistic_clusters")
            if not name or not isinstance(logistics, list):
                raise OzonApiError("Кластер Ozon без названия или списка складов")
            for logistic in logistics:
                warehouses = logistic.get("warehouses") if isinstance(logistic, dict) else None
                if not isinstance(warehouses, list):
                    raise OzonApiError("Неизвестный формат складов кластера Ozon")
                for warehouse in warehouses:
                    if not isinstance(warehouse, dict):
                        raise OzonApiError("Неизвестный формат склада кластера Ozon")
                    if warehouse.get("type") != "FULL_FILLMENT":
                        continue
                    warehouse_name = str(warehouse.get("name") or "").strip()
                    if not warehouse_name:
                        raise OzonApiError("Склад FBO в кластере Ozon без названия")
                    if warehouse_name in mapping and mapping[warehouse_name] != name:
                        raise OzonApiError("Ozon отнёс склад к нескольким кластерам")
                    mapping[warehouse_name] = name
                    warehouse_id = warehouse.get("warehouse_id")
                    if warehouse_id is not None:
                        id_key = f"id:{warehouse_id}"
                        if id_key in mapping and mapping[id_key] != name:
                            raise OzonApiError("Ozon отнёс склад к нескольким кластерам")
                        mapping[id_key] = name
        self._cluster_cache[kind] = (monotonic() + 24 * 60 * 60, mapping)
        return mapping.copy()

    async def _get_fbo_warehouse_inventory(
        self, skus: set[str]
    ) -> list[WarehouseInventory]:
        rows: list[WarehouseInventory] = []
        seen: set[tuple[str, str]] = set()
        offset = 0
        limit = 1000
        for _ in range(1000):
            data = await self._post(
                "/v2/analytics/stock_on_warehouses",
                {"limit": limit, "offset": offset, "warehouse_type": "ALL"},
            )
            result = data.get("result")
            page = result.get("rows") if isinstance(result, dict) else None
            if not isinstance(page, list):
                raise OzonApiError("Неизвестный формат остатков по складам Ozon")
            for item in page:
                if not isinstance(item, dict):
                    raise OzonApiError("Неизвестная строка остатков по складам Ozon")
                sku = str(item.get("sku") or "")
                if sku not in skus:
                    continue
                warehouse = str(item.get("warehouse_name") or "").strip()
                if not warehouse:
                    raise OzonApiError("Ozon вернул остаток без названия склада")
                key = (sku, warehouse)
                if key in seen:
                    raise OzonApiError("Ozon вернул повторный остаток по складу FBO")
                seen.add(key)
                stock = nonnegative_int(item.get("free_to_sell_amount"), "free_to_sell_amount")
                transit = nonnegative_int(item.get("promised_amount"), "promised_amount")
                nonnegative_int(item.get("reserved_amount"), "reserved_amount")
                if stock or transit:
                    rows.append(WarehouseInventory(sku, warehouse, "FBO", stock, transit))
            if len(page) < limit:
                return rows
            offset += limit
        raise OzonApiError("Слишком много строк остатков по складам Ozon")

    async def _get_fbs_warehouse_inventory(
        self, skus: set[str]
    ) -> list[WarehouseInventory]:
        rows: list[WarehouseInventory] = []
        seen: set[tuple[str, str]] = set()
        ordered = sorted(skus)
        for start in range(0, len(ordered), 1000):
            chunk = ordered[start : start + 1000]
            cursor = ""
            cursors: set[str] = set()
            for _ in range(1000):
                body: dict[str, Any] = {"sku": chunk, "limit": 1000}
                if cursor:
                    body["cursor"] = cursor
                data = await self._post("/v2/product/info/stocks-by-warehouse/fbs", body)
                page = data.get("products")
                has_next = data.get("has_next")
                if not isinstance(page, list) or not isinstance(has_next, bool):
                    raise OzonApiError("Неизвестный формат остатков по складам FBS")
                for item in page:
                    if not isinstance(item, dict):
                        raise OzonApiError("Неизвестная строка остатков по складам FBS")
                    sku = str(item.get("sku") or "")
                    if sku not in skus:
                        raise OzonApiError("Ozon вернул остаток FBS с неизвестным SKU")
                    warehouse = str(item.get("warehouse_name") or "").strip()
                    if not warehouse:
                        raise OzonApiError("Ozon вернул остаток FBS без названия склада")
                    key = (sku, warehouse)
                    if key in seen:
                        raise OzonApiError("Ozon вернул повторный остаток по складу FBS")
                    seen.add(key)
                    stock = nonnegative_int(item.get("present"), "present")
                    nonnegative_int(item.get("reserved"), "reserved")
                    nonnegative_int(item.get("free_stock"), "free_stock")
                    if stock:
                        rows.append(WarehouseInventory(sku, warehouse, "FBS", stock, 0))
                if not has_next:
                    break
                next_cursor = str(data.get("cursor") or "")
                if not page or not next_cursor or next_cursor in cursors:
                    raise OzonApiError("Ozon не вернул следующую страницу остатков FBS")
                cursors.add(next_cursor)
                cursor = next_cursor
            else:
                raise OzonApiError("Слишком много страниц остатков по складам FBS")
        return rows

    async def _get_available_inventory(
        self, product_ids: set[str], skus: set[str]
    ) -> dict[str, int]:
        """Return current saleable FBO stock; exclude FBS, rFBS, and reservations."""
        totals: dict[str, int] = defaultdict(int)
        ordered_ids = sorted(product_ids)
        seen_products: set[str] = set()
        seen_stocks: set[tuple[str, str, str, str]] = set()
        for start in range(0, len(ordered_ids), 1000):
            chunk = ordered_ids[start : start + 1000]
            cursor = ""
            cursors: set[str] = set()
            chunk_seen: set[str] = set()
            for _ in range(1000):
                data = await self._post(
                    "/v4/product/info/stocks",
                    {
                        "filter": {"product_id": chunk, "visibility": "ALL"},
                        "cursor": cursor,
                        "limit": 1000,
                    },
                )
                items = data.get("items")
                total = data.get("total")
                if not isinstance(items, list) or isinstance(total, bool) or not isinstance(total, int):
                    raise OzonApiError("Неизвестный формат актуальных остатков Ozon")
                if total < 0:
                    raise OzonApiError("Ozon вернул отрицательное число товаров с остатками")
                for item in items:
                    if not isinstance(item, dict):
                        raise OzonApiError("Неизвестный формат строки актуальных остатков")
                    product_id = str(item.get("product_id", ""))
                    if product_id not in chunk or product_id in seen_products:
                        raise OzonApiError("Ozon вернул повторный или неизвестный товар в остатках")
                    seen_products.add(product_id)
                    chunk_seen.add(product_id)
                    stocks = item.get("stocks")
                    if not isinstance(stocks, list):
                        raise OzonApiError("Ozon не вернул остатки товара по схемам продаж")
                    for stock in stocks:
                        if not isinstance(stock, dict):
                            raise OzonApiError("Неизвестный формат остатка по схеме продаж")
                        stock_type = str(stock.get("type", "")).lower()
                        if stock_type != "fbo":
                            continue
                        sku = str(stock.get("sku", ""))
                        if sku not in skus:
                            raise OzonApiError("Ozon вернул остаток с неизвестным SKU")
                        shipment_type = str(stock.get("shipment_type", ""))
                        key = (product_id, sku, stock_type, shipment_type)
                        if key in seen_stocks:
                            raise OzonApiError("Ozon вернул повторный остаток по схеме продаж")
                        seen_stocks.add(key)
                        totals[sku] += nonnegative_int(stock.get("present"), "present")
                        nonnegative_int(stock.get("reserved"), "reserved")
                if len(chunk_seen) >= total:
                    break
                next_cursor = str(data.get("cursor") or "")
                if not items or not next_cursor or next_cursor in cursors:
                    raise OzonApiError("Ozon не вернул следующую страницу актуальных остатков")
                cursors.add(next_cursor)
                cursor = next_cursor
            else:
                raise OzonApiError("Слишком много страниц актуальных остатков")
        return dict(totals)

    async def _get_fbo_transit(self, skus: set[str]) -> dict[str, int]:
        """Return products in active FBO supplies before warehouse acceptance finishes."""
        totals: dict[str, int] = defaultdict(int)
        order_ids: list[str] = []
        last_id = ""
        seen_pages: set[str] = set()
        for _ in range(1000):
            data = await self._post(
                "/v3/supply-order/list",
                {
                    "filter": {"states": sorted(FBO_TRANSIT_STATES)},
                    "last_id": last_id,
                    "limit": 100,
                    "sort_by": "ORDER_STATE_UPDATED_AT",
                    "sort_dir": "DESC",
                },
            )
            page_ids = data.get("order_ids")
            if not isinstance(page_ids, list):
                raise OzonApiError("Неизвестный формат списка поставок FBO")
            parsed_ids = [str(order_id) for order_id in page_ids]
            if any(not order_id for order_id in parsed_ids):
                raise OzonApiError("Ozon вернул поставку без идентификатора")
            order_ids.extend(parsed_ids)
            next_id = str(data.get("last_id") or "")
            if len(page_ids) < 100 or not next_id:
                break
            if next_id in seen_pages:
                raise OzonApiError("Ozon повторил страницу списка поставок FBO")
            seen_pages.add(next_id)
            last_id = next_id
        else:
            raise OzonApiError("Слишком много страниц поставок FBO")

        if len(order_ids) != len(set(order_ids)):
            raise OzonApiError("Ozon вернул повторную заявку на поставку FBO")

        bundle_ids: list[str] = []
        seen_supplies: set[str] = set()
        for start in range(0, len(order_ids), 100):
            chunk = order_ids[start : start + 100]
            data = await self._post("/v3/supply-order/get", {"order_ids": chunk})
            orders = data.get("orders")
            if not isinstance(orders, list):
                raise OzonApiError("Неизвестный формат заявок на поставку FBO")
            returned_ids: set[str] = set()
            for order in orders:
                if not isinstance(order, dict):
                    raise OzonApiError("Неизвестный формат заявки на поставку FBO")
                order_id = str(order.get("order_id") or "")
                if order_id not in chunk or order_id in returned_ids:
                    raise OzonApiError("Ozon вернул неизвестную или повторную заявку FBO")
                returned_ids.add(order_id)
                supplies = order.get("supplies")
                if not isinstance(supplies, list):
                    raise OzonApiError("Ozon не вернул поставки в заявке FBO")
                for supply in supplies:
                    if not isinstance(supply, dict):
                        raise OzonApiError("Неизвестный формат поставки FBO")
                    if str(supply.get("state") or "") not in FBO_TRANSIT_STATES:
                        continue
                    supply_id = str(supply.get("supply_id") or "")
                    bundle_id = str(supply.get("bundle_id") or "")
                    if not supply_id or not bundle_id:
                        raise OzonApiError("В активной поставке FBO отсутствует состав")
                    if supply_id in seen_supplies:
                        raise OzonApiError("Ozon вернул повторную поставку FBO")
                    seen_supplies.add(supply_id)
                    bundle_ids.append(bundle_id)
            if returned_ids != set(chunk):
                raise OzonApiError("Ozon вернул неполные данные по заявкам FBO")

        if len(bundle_ids) != len(set(bundle_ids)):
            raise OzonApiError("Ozon вернул повторный состав поставки FBO")

        active_bundle_ids = set(bundle_ids)
        self._bundle_cache = {
            bundle_id: quantities
            for bundle_id, quantities in self._bundle_cache.items()
            if bundle_id in active_bundle_ids
        }
        for bundle_id in bundle_ids:
            quantities = self._bundle_cache.get(bundle_id)
            if quantities is None:
                quantities = await self._get_fbo_bundle(bundle_id)
                self._bundle_cache[bundle_id] = quantities
            for sku, quantity in quantities.items():
                if sku in skus:
                    totals[sku] += quantity
        return dict(totals)

    async def _get_fbo_bundle(self, bundle_id: str) -> dict[str, int]:
        """Download one immutable FBO supply composition."""
        totals: dict[str, int] = defaultdict(int)
        last_id = ""
        seen_pages: set[str] = set()
        for _ in range(1000):
            data = await self._post(
                "/v1/supply-order/bundle",
                {
                    "bundle_ids": [bundle_id],
                    "is_asc": True,
                    "last_id": last_id,
                    "limit": 100,
                    "query": "",
                    "sort_field": "SKU",
                },
            )
            items = data.get("items")
            has_next = data.get("has_next")
            if not isinstance(items, list) or not isinstance(has_next, bool):
                raise OzonApiError("Неизвестный формат состава поставки FBO")
            for item in items:
                if not isinstance(item, dict):
                    raise OzonApiError("Неизвестный формат товара в поставке FBO")
                sku = str(item.get("sku") or "")
                if not sku:
                    raise OzonApiError("В составе поставки FBO отсутствует SKU")
                totals[sku] += nonnegative_int(item.get("quantity"), "quantity")
            if not has_next:
                return dict(totals)
            next_id = str(data.get("last_id") or "")
            if not items or not next_id or next_id in seen_pages:
                raise OzonApiError("Ozon не вернул следующую страницу состава поставки FBO")
            seen_pages.add(next_id)
            last_id = next_id
        raise OzonApiError("Слишком много товаров в поставке FBO")
