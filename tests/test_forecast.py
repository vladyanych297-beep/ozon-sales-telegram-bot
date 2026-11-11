import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import date
from decimal import ROUND_UP, Decimal
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from ozon_forecast.api import create_app
from ozon_forecast.client import ForecastOzonClient, OzonRateLimitError, retry_after_seconds
from ozon_forecast.models import Inventory, Product, WarehouseInventory, units_to_supply
from ozon_forecast.service import ForecastService, combine_rows, combine_warehouse_rows
from ozon_forecast.settings import ServerSettings
from ozon_sales_bot.models import SaleRow
from ozon_sales_bot.ozon import OzonApiError


@pytest.mark.parametrize(
    "sales,days,stock,transit",
    [
        (30, 30, 5, 10),
        (1, 1, 0, 0),
        (0, 30, 10, 10),
        (100, 45, 70, 20),
        (7, 30, 2, 0),
        (1, 29, 1, 0),
        (10**12, 365, 0, 0),
    ],
)
def test_exact_excel_formula(sales, days, stock, transit):
    expected = max(
        0, int((Decimal(sales) / 30 * days - stock - transit).to_integral_value(rounding=ROUND_UP))
    )
    assert units_to_supply(sales, days, stock, transit) == expected


def test_formula_uses_selected_sales_period():
    expected = max(
        0, int((Decimal(30) / 15 * 20 - 3 - 2).to_integral_value(rounding=ROUND_UP))
    )
    assert units_to_supply(30, 20, 3, 2, 15) == expected == 35


@pytest.mark.parametrize(
    "values",
    [
        (1, 0, 0, 0),
        (1, 366, 0, 0),
        (-1, 30, 0, 0),
        (1, 30, -1, 0),
        (1.5, 30, 0, 0),
        (True, 30, 0, 0),
    ],
)
def test_invalid_formula_inputs(values):
    with pytest.raises(ValueError):
        units_to_supply(*values)


def test_cancelled_alias_and_invalid_sales_quantity():
    postings = [
        {"status": "delivered", "products": [{"sku": 10, "quantity": 3}]},
        {
            "status": "unknown",
            "status_alias": "cancelled",
            "products": [{"sku": 10, "quantity": 100}],
        },
    ]
    assert ForecastOzonClient._aggregate_postings(postings, None)[0].ordered_units == 3
    with pytest.raises(OzonApiError, match="quantity"):
        ForecastOzonClient._aggregate_postings([{"products": [{"sku": 10}]}], None)


def test_aliases_and_products_without_sales():
    rows = combine_rows(
        [
            Product("1", "article", "Товар", frozenset({"10", "11"})),
            Product("2", "new", "Новый", frozenset({"20"})),
        ],
        [SaleRow("10", "FBO", 10), SaleRow("11", "FBS", 20)],
        {"10": Inventory(5, 7), "20": Inventory(50, 0)},
        30,
    )
    assert [(r.sales_30_days, r.stock, r.in_transit, r.to_supply) for r in rows] == [
        (30, 5, 7, 18),
        (0, 50, 0, 0),
    ]
    with pytest.raises(OzonApiError, match="отсутствующий"):
        combine_rows([], [SaleRow("lost", "Unknown", 5)], {}, 30)


def test_forecast_rows_include_sales_stock_and_transit_by_cluster():
    product = Product("1", "A", "Товар", frozenset({"10", "11"}))
    rows = combine_rows(
        [product],
        [SaleRow("10", "Товар", 4, "Санкт-Петербург", "FBO"),
         SaleRow("11", "Товар", 2, "Москва", "FBS")],
        {"10": Inventory(7, 3), "11": Inventory(5, 0)},
        30, 30,
        [WarehouseInventory("10", "Москва", "FBO", 7, 3),
         WarehouseInventory("11", "Склады продавца", "FBS", 5, 0)],
    )
    assert rows[0].to_supply == 0
    assert [(c.name, c.scheme, c.sales, c.stock, c.in_transit)
            for c in rows[0].clusters] == [
        ("Санкт-Петербург", "FBO", 4, 0, 0),
        ("Москва", "FBO", 2, 7, 3),
    ]


def test_clusters_are_sorted_by_sales_stock_and_transit_descending():
    product = Product("1", "A", "Товар", frozenset({"10"}))
    rows = combine_rows(
        [product],
        [SaleRow("10", "Товар", 5, "В", "FBO"),
         SaleRow("10", "Товар", 5, "Б", "FBO"),
         SaleRow("10", "Товар", 5, "А", "FBO"),
         SaleRow("10", "Товар", 6, "Г", "FBO")],
        {"10": Inventory()},
        30, 30,
        [WarehouseInventory("10", "В", "FBO", 3, 8),
         WarehouseInventory("10", "Б", "FBO", 4, 1),
         WarehouseInventory("10", "А", "FBO", 4, 2)],
    )
    assert [(c.name, c.sales, c.stock, c.in_transit) for c in rows[0].clusters] == [
        ("Г", 6, 0, 0),
        ("А", 5, 4, 2),
        ("Б", 5, 4, 1),
        ("В", 5, 3, 8),
    ]


@pytest.mark.asyncio
async def test_sales_use_destination_cluster_for_fbo_and_fbs():
    client = ForecastOzonClient("test", "test", "Europe/Moscow")
    client._get_fbo_postings = AsyncMock(return_value=[
        {"status": "delivered", "analytics_data": {"warehouse_name": "Москва", "warehouse_id": 123},
         "financial_data": {"cluster_from": "Москва", "cluster_to": "Санкт-Петербург"},
         "products": [{"sku": 10, "name": "Товар", "quantity": 3}]},
        {"status": "delivered", "financial_data": {"cluster_to": "Казань"},
         "products": [{"sku": 10, "quantity": 2}]},
        {"status": "cancelled", "products": [{"sku": 10, "quantity": 99}]},
    ])
    client._get_fbs_postings = AsyncMock(return_value=[
        {"posting_number": "fbs-1", "status": "delivered", "financial_data": {"cluster_from": "Москва", "cluster_to": "Казань"},
         "products": [{"sku": 10, "quantity": 4}]},
    ])
    client._get_returned_quantities = AsyncMock(return_value={("fbs-1", "10"): 1})
    client._get_cluster_warehouse_map = AsyncMock()
    sales = await client.get_sales(date(2026, 9, 1), date(2026, 9, 30))
    assert [(x.scheme, x.cluster, x.ordered_units) for x in sales] == [
        ("FBO", "Казань", 5),
        ("FBO", "Санкт-Петербург", 3),
    ]
    client._get_cluster_warehouse_map.assert_not_awaited()
    await client.close()


@pytest.mark.asyncio
async def test_forecast_requests_destination_cluster_fields():
    client = ForecastOzonClient("test", "test", "Europe/Moscow")
    calls = []

    async def post(endpoint, body):
        calls.append((endpoint, body))
        if endpoint == "/v1/returns/list":
            return {"returns": [], "has_next": False}
        return {"postings": [], "has_next": False} if "fbo" in endpoint else {
            "result": {"postings": [], "has_next": False}
        }

    client._post = AsyncMock(side_effect=post)
    await client.get_sales(date(2026, 9, 1), date(2026, 9, 30))
    assert len(calls) == 3
    assert all(
        body["with"]["financial_data"] is True
        for endpoint, body in calls if "posting" in endpoint
    )
    await client.close()


@pytest.mark.asyncio
async def test_returned_units_are_paginated_and_subtracted_from_original_posting():
    client = ForecastOzonClient("test", "test", "Europe/Moscow")
    client._get_fbo_postings = AsyncMock(return_value=[
        {"posting_number": "fbo-1", "status": "delivered",
         "financial_data": {"cluster_to": "Москва"},
         "products": [{"sku": 10, "name": "Товар", "quantity": 3}]},
        {"posting_number": "fbo-2", "status": "delivered",
         "financial_data": {"cluster_to": "Казань"},
         "products": [{"sku": 10, "name": "Товар", "quantity": 2}]},
    ])
    client._get_fbs_postings = AsyncMock(return_value=[])

    async def post(endpoint, body):
        assert endpoint == "/v1/returns/list"
        assert body["limit"] == 500
        assert set(body["filter"]) == {"logistic_return_date"}
        if body["last_id"] == 0:
            return {
                "returns": [{
                    "id": 10, "posting_number": "fbo-1",
                    "product": {"sku": 10, "quantity": 1},
                }],
                "has_next": True,
            }
        assert body["last_id"] == 10
        return {
            "returns": [
                {"id": 11, "posting_number": "fbo-1",
                 "product": {"sku": 10, "quantity": 2}},
                # The order is outside the selected postings and must not affect sales.
                {"id": 12, "posting_number": "old-order",
                 "product": {"sku": 10, "quantity": 100}},
            ],
            "has_next": False,
        }

    client._post = AsyncMock(side_effect=post)
    sales = await client.get_sales(date(2026, 9, 1), date(2026, 9, 30))
    assert [(row.cluster, row.ordered_units) for row in sales] == [("Казань", 2)]
    assert client._post.await_count == 2
    await client.close()


def test_warehouse_inventory_is_grouped_by_product_and_cluster():
    products = [Product("1", "A", "Товар", frozenset({"10", "11"}))]
    rows = combine_warehouse_rows(products, [
        WarehouseInventory("10", "Москва, МО и Дальние регионы", "FBO", 4, 2),
        WarehouseInventory("11", "Москва, МО и Дальние регионы", "FBO", 3, 1),
        WarehouseInventory("10", "Склады продавца", "FBS", 5, 0),
    ])
    assert len(rows) == 1
    assert [(x.warehouse_name, x.scheme, x.stock, x.in_transit) for x in rows[0].warehouses] == [
        ("Москва, МО и Дальние регионы", "FBO", 7, 3),
    ]


@pytest.mark.asyncio
async def test_detailed_fbo_and_fbs_warehouse_inventory():
    client = ForecastOzonClient("test", "test", "Europe/Moscow")

    async def post(endpoint, body):
        if endpoint == "/v2/analytics/stock_on_warehouses":
            assert body == {"limit": 1000, "offset": 0, "warehouse_type": "ALL"}
            return {"result": {"rows": [
                {"sku": 10, "warehouse_name": "FBO Москва 1", "free_to_sell_amount": 8,
                 "promised_amount": 4, "reserved_amount": 1},
                {"sku": 10, "warehouse_name": "FBO Москва 2", "free_to_sell_amount": 2,
                 "promised_amount": 1, "reserved_amount": 0},
                {"sku": 20, "warehouse_name": "Пустой", "free_to_sell_amount": 0,
                 "promised_amount": 0, "reserved_amount": 0},
            ]}}
        if endpoint == "/v1/cluster/list":
            assert body == {"cluster_type": "CLUSTER_TYPE_OZON"}
            return {"clusters": [{"name": "Москва", "logistic_clusters": [{"warehouses": [
                {"name": "FBO Москва 1", "type": "FULL_FILLMENT"},
                {"name": "FBO Москва 2", "type": "FULL_FILLMENT"},
            ]}]}]}
        assert endpoint == "/v2/product/info/stocks-by-warehouse/fbs"
        assert body == {"sku": ["10", "20"], "limit": 1000}
        return {"products": [
            {"sku": 10, "warehouse_name": "FBS Основной", "present": 3,
             "reserved": 1, "free_stock": 2},
            {"sku": 20, "warehouse_name": "FBS Пустой", "present": 0,
             "reserved": 0, "free_stock": 0},
        ], "has_next": False, "cursor": ""}

    client._post = AsyncMock(side_effect=post)
    result = await client.get_warehouse_inventory([
        Product("1", "A", "Первый", frozenset({"10"})),
        Product("2", "B", "Второй", frozenset({"20"})),
    ])
    assert result == [
        WarehouseInventory("10", "Москва", "FBO", 8, 4),
        WarehouseInventory("10", "Москва", "FBO", 2, 1),
    ]
    grouped = combine_warehouse_rows([Product("1", "A", "Первый", frozenset({"10"}))], result)
    assert [(row.warehouse_name, row.stock, row.in_transit) for row in grouped[0].warehouses] == [
        ("Москва", 10, 5),
    ]
    await client.close()


@pytest.mark.asyncio
async def test_stock_warehouses_and_active_fbo_supplies():
    async def handler(request):
        body = json.loads(request.content)
        if request.url.path == "/v4/product/info/stocks":
            assert body == {
                "filter": {"product_id": ["1"], "visibility": "ALL"},
                "cursor": "",
                "limit": 1000,
            }
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "product_id": 1,
                            "offer_id": "A",
                            "stocks": [
                                {
                                    "sku": 10,
                                    "type": "fbo",
                                    "present": 10,
                                    "reserved": 2,
                                    "shipment_type": "SHIPMENT_TYPE_GENERAL",
                                },
                                {
                                    "sku": 10,
                                    "type": "fbs",
                                    "present": 4,
                                    "reserved": 1,
                                    "shipment_type": "SHIPMENT_TYPE_GENERAL",
                                },
                            ],
                        }
                    ],
                    "cursor": "",
                    "total": 1,
                },
            )
        if request.url.path == "/v3/supply-order/list":
            assert body["filter"]["states"] == [
                "ACCEPTANCE_AT_STORAGE_WAREHOUSE",
                "ACCEPTED_AT_SUPPLY_WAREHOUSE",
                "IN_TRANSIT",
                "READY_TO_SUPPLY",
            ]
            return httpx.Response(200, json={"order_ids": ["7"], "last_id": ""})
        if request.url.path == "/v3/supply-order/get":
            assert body == {"order_ids": ["7"]}
            return httpx.Response(
                200,
                json={
                    "orders": [
                        {
                            "order_id": 7,
                            "supplies": [
                                {
                                    "supply_id": 70,
                                    "bundle_id": "active",
                                    "state": "IN_TRANSIT",
                                },
                                {
                                    "supply_id": 71,
                                    "bundle_id": "completed",
                                    "state": "COMPLETED",
                                },
                            ],
                        }
                    ]
                },
            )
        assert request.url.path == "/v1/supply-order/bundle"
        assert body["bundle_ids"] == ["active"]
        return httpx.Response(
            200,
            json={
                "items": [
                    {"sku": 10, "quantity": 7},
                    {"sku": 999, "quantity": 100},
                ],
                "has_next": False,
                "last_id": "",
            },
        )

    client = ForecastOzonClient("test", "test", "Europe/Moscow")
    await client.close()
    client._http = httpx.AsyncClient(
        base_url="https://example.test", transport=httpx.MockTransport(handler)
    )
    products = [Product("1", "A", "Товар", frozenset({"10"}))]
    assert await client.get_inventory(products) == {"10": Inventory(10, 7)}
    await client.close()


@pytest.mark.asyncio
async def test_stock_missing_field_is_error_not_zero():
    client = ForecastOzonClient("test", "test", "Europe/Moscow")
    async def post(endpoint, body):
        if endpoint == "/v3/supply-order/list":
            return {"order_ids": [], "last_id": ""}
        return {
            "items": [
                {
                    "product_id": 1,
                    "stocks": [
                        {
                            "sku": 10,
                            "type": "fbo",
                            "present": 3,
                            "shipment_type": "SHIPMENT_TYPE_GENERAL",
                        }
                    ],
                }
            ],
            "cursor": "",
            "total": 1,
        }

    client._post = AsyncMock(side_effect=post)
    with pytest.raises(OzonApiError, match="reserved"):
        await client.get_inventory([Product("1", "A", "Товар", frozenset({"10"}))])
    await client.close()


@pytest.mark.asyncio
async def test_fbo_inventory_pagination_ignores_fbs_without_supplies():
    client = ForecastOzonClient("test", "test", "Europe/Moscow")

    async def post(endpoint, body):
        if endpoint == "/v3/supply-order/list":
            return {"order_ids": [], "last_id": ""}
        if body["cursor"] == "":
            return {
                "items": [
                    {
                        "product_id": 1,
                        "stocks": [
                            {
                                "sku": 10,
                                "type": "fbo",
                                "present": 3,
                                "reserved": 1,
                                "shipment_type": "SHIPMENT_TYPE_GENERAL",
                            }
                        ],
                    }
                ],
                "cursor": "next",
                "total": 2,
            }
        return {
            "items": [
                {
                    "product_id": 2,
                    "stocks": [
                        {
                            "sku": 20,
                            "type": "fbs",
                            "present": 4,
                            "reserved": 2,
                            "shipment_type": "SHIPMENT_TYPE_GENERAL",
                        }
                    ],
                }
            ],
            "cursor": "",
            "total": 2,
        }

    client._post = AsyncMock(side_effect=post)
    products = [
        Product("1", "A", "Первый", frozenset({"10"})),
        Product("2", "B", "Второй", frozenset({"20"})),
    ]
    assert await client.get_inventory(products) == {
        "10": Inventory(3, 0),
    }
    assert client._post.await_count == 3
    await client.close()


@pytest.mark.asyncio
async def test_fbo_supply_bundle_pagination_and_state_filtering():
    client = ForecastOzonClient("test", "test", "Europe/Moscow")

    async def post(endpoint, body):
        if endpoint == "/v3/supply-order/list":
            return {"order_ids": ["1"], "last_id": ""}
        if endpoint == "/v3/supply-order/get":
            return {
                "orders": [
                    {
                        "order_id": 1,
                        "supplies": [
                            {"supply_id": 10, "bundle_id": "a", "state": "READY_TO_SUPPLY"},
                            {"supply_id": 11, "bundle_id": "b", "state": "CANCELLED"},
                        ],
                    }
                ]
            }
        assert endpoint == "/v1/supply-order/bundle"
        assert body["bundle_ids"] == ["a"]
        if body["last_id"] == "":
            return {
                "items": [{"sku": 10, "quantity": 3}],
                "has_next": True,
                "last_id": "10",
            }
        return {
            "items": [{"sku": 10, "quantity": 4}],
            "has_next": False,
            "last_id": "",
        }

    client._post = AsyncMock(side_effect=post)
    assert await client._get_fbo_transit({"10"}) == {"10": 7}
    assert client._post.await_count == 4
    await client.close()


@pytest.mark.asyncio
async def test_fbo_supply_bundle_is_reused_while_active():
    client = ForecastOzonClient("test", "test", "Europe/Moscow")

    async def post(endpoint, body):
        if endpoint == "/v3/supply-order/list":
            return {"order_ids": ["1"], "last_id": ""}
        if endpoint == "/v3/supply-order/get":
            return {
                "orders": [
                    {
                        "order_id": 1,
                        "supplies": [
                            {"supply_id": 10, "bundle_id": "stable", "state": "IN_TRANSIT"}
                        ],
                    }
                ]
            }
        assert endpoint == "/v1/supply-order/bundle"
        return {
            "items": [{"sku": 10, "quantity": 5}],
            "has_next": False,
            "last_id": "",
        }

    client._post = AsyncMock(side_effect=post)
    assert await client._get_fbo_transit({"10"}) == {"10": 5}
    assert await client._get_fbo_transit({"10"}) == {"10": 5}
    assert [call.args[0] for call in client._post.await_args_list].count(
        "/v1/supply-order/bundle"
    ) == 1
    await client.close()


def make_service(tmp_path):
    settings = ServerSettings("client", "secret", snapshot_path=tmp_path / "snapshot.json")
    client = AsyncMock()
    client.get_catalog.return_value = [Product("1", "A", "Товар", frozenset({"10"}))]
    client.get_sales.return_value = [SaleRow("10", "Товар", 30)]
    client.get_inventory.return_value = {"10": Inventory(2, 3)}
    client.get_warehouse_inventory.return_value = [
        WarehouseInventory("10", "FBO Москва", "FBO", 2, 3)
    ]
    return ForecastService(settings, client), client


@pytest.mark.asyncio
async def test_cache_coalesces_requests_and_persists(tmp_path):
    service, client = make_service(tmp_path)
    a, b = await asyncio.gather(service.report(30), service.report(60))
    assert a.rows[0].to_supply == 25
    assert b.rows[0].to_supply == 55
    client.get_catalog.assert_awaited_once()
    service._loaded_at = 0
    client.get_inventory.side_effect = OzonApiError("Нет прав")
    stale = await service.report(10)
    assert stale.stale and stale.warning == "Нет прав"
    assert stale.fetched_at == a.fetched_at
    assert stale.rows[0].to_supply == 5
    reloaded = ForecastService(service.settings, client)
    report = await reloaded.report(30)
    assert report.stale
    assert report.rows[0].to_supply == 25


@pytest.mark.asyncio
async def test_old_outgoing_cluster_snapshot_is_not_reused(tmp_path):
    service, client = make_service(tmp_path)
    await service.report(30)
    saved = json.loads(service.settings.snapshot_path.read_text(encoding="utf-8"))
    assert saved.pop("cluster_sales_direction") == "to"
    service.settings.snapshot_path.write_text(json.dumps(saved), encoding="utf-8")
    reloaded = ForecastService(service.settings, client)
    assert reloaded._snapshot is None


@pytest.mark.asyncio
async def test_snapshot_without_fbo_only_markers_is_not_reused(tmp_path):
    service, client = make_service(tmp_path)
    await service.report(30)
    saved = json.loads(service.settings.snapshot_path.read_text(encoding="utf-8"))
    assert saved.pop("cluster_inventory_scope") == "fbo_only"
    assert saved.pop("cluster_sales_scheme") == "fbo_combined"
    service.settings.snapshot_path.write_text(json.dumps(saved), encoding="utf-8")
    reloaded = ForecastService(service.settings, client)
    assert reloaded._snapshot is None


@pytest.mark.asyncio
async def test_cache_is_separate_for_each_sales_period(tmp_path):
    service, client = make_service(tmp_path)
    first = await service.report(30, 30)
    second = await service.report(30, 15)
    assert first.sales_period_days == 30
    assert second.sales_period_days == 15
    assert second.rows[0].to_supply == 55
    assert client.get_catalog.await_count == 2
    assert client.get_sales.await_count == 2


def test_http_validation_and_upstream_error(tmp_path):
    service, client = make_service(tmp_path)
    client.get_catalog.side_effect = OzonApiError("Нет прав на остатки")
    with TestClient(create_app(service)) as http:
        assert http.get("/health").json()["status"] == "ok"
        assert http.get("/api/v1/forecast?days=0").status_code == 422
        error = http.get("/api/v1/forecast")
        assert error.status_code == 503
        assert error.json()["detail"] == "Нет прав на остатки"
        assert "secret" not in error.text
        assert http.get("/api/v1/forecast").status_code == 503
        client.get_catalog.assert_awaited_once()


def test_http_default_and_report_schema(tmp_path):
    service, _ = make_service(tmp_path)
    with TestClient(create_app(service)) as http:
        result = http.get("/api/v1/forecast")
        assert result.status_code == 200
        assert result.headers["cache-control"] == "no-store"
        report = result.json()
        assert report["period_days"] == 30
        assert report["rows"][0]["to_supply"] == 25
        assert (
            date.fromisoformat(report["sales_date_to"])
            - date.fromisoformat(report["sales_date_from"])
        ).days == 29


def test_http_warehouse_stock_schema_and_cache(tmp_path):
    service, client = make_service(tmp_path)
    with TestClient(create_app(service)) as http:
        first = http.get("/api/v1/warehouse-stocks")
        second = http.get("/api/v1/warehouse-stocks")
    assert first.status_code == second.status_code == 200
    report = first.json()
    assert report["rows"][0]["warehouses"] == [{
        "warehouse_name": "FBO Москва", "scheme": "FBO", "stock": 2, "in_transit": 3
    }]
    client.get_warehouse_inventory.assert_awaited_once()


def test_public_update_metadata_and_apk(tmp_path):
    service, _ = make_service(tmp_path)
    apk = tmp_path / "ozon-plan.apk"
    service.settings = replace(service.settings, update_apk_path=apk)
    with TestClient(create_app(service)) as http:
        assert http.get("/api/v1/update").status_code == 404
        apk.write_bytes(b"signed-apk-test")
        info = http.get("/api/v1/update")
        assert info.status_code == 200
        assert info.json() == {
            "version_code": 34,
            "version_name": "0.7.8",
            "size_bytes": len(b"signed-apk-test"),
            "sha256": hashlib.sha256(b"signed-apk-test").hexdigest(),
        }
        binary = http.get("/api/v1/update/apk")
        assert binary.status_code == 200
        assert binary.content == b"signed-apk-test"
        assert binary.headers["cache-control"] == "no-store"
        assert binary.headers["content-type"] == "application/vnd.android.package-archive"
        part = http.get("/api/v1/update/apk", headers={"Range": "bytes=2-7"})
        assert part.status_code == 206
        assert part.content == b"gned-a"
        assert part.headers["content-range"] == "bytes 2-7/15"


def test_http_uses_selected_sales_and_forecast_periods(tmp_path):
    service, client = make_service(tmp_path)
    with TestClient(create_app(service)) as http:
        result = http.get("/api/v1/forecast?days=20&sales_days=15")
        assert result.status_code == 200
        report = result.json()
        assert report["period_days"] == 20
        assert report["sales_period_days"] == 15
        assert report["rows"][0]["to_supply"] == 35
        requested_from, requested_to = client.get_sales.await_args.args
        assert (requested_to - requested_from).days == 14
        assert http.get("/api/v1/forecast?sales_days=0").status_code == 422


@pytest.mark.asyncio
async def test_moscow_day_is_converted_to_utc():
    client = ForecastOzonClient("test", "test", "Europe/Moscow")
    assert client._date_bounds(date(2026, 8, 1), date(2026, 8, 30)) == (
        "2026-07-31T21:00:00.000Z",
        "2026-08-30T20:59:59.999Z",
    )
    await client.close()


@pytest.mark.asyncio
async def test_catalog_pagination_and_sku_aliases():
    client = ForecastOzonClient("test", "test", "Europe/Moscow")
    client._post = AsyncMock(
        side_effect=[
            {"result": {"items": [{"product_id": 1}], "total": 2, "last_id": "next"}},
            {"result": {"items": [{"product_id": 2}], "total": 2, "last_id": "end"}},
            {
                "items": [
                    {"id": 1, "offer_id": "a", "name": "A", "sku": 10, "sources": [{"sku": 11}]},
                    {"id": 2, "offer_id": "b", "name": "B", "sources": [{"sku": 20}]},
                ]
            },
        ]
    )
    products = await client.get_catalog()
    assert products[0].skus == frozenset({"10", "11"})
    assert client._post.call_args_list[1].args[1]["last_id"] == "next"
    await client.close()


@pytest.mark.asyncio
async def test_rate_limit_stops_immediate_retries(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(429, headers={"Retry-After": "120"}, json={"ignored": "secret"})

    client = ForecastOzonClient("test", "test", "Europe/Moscow")
    await client.close()
    client._http = httpx.AsyncClient(
        base_url="https://example.test", transport=httpx.MockTransport(handler)
    )
    sleeper = AsyncMock()
    monkeypatch.setattr("ozon_forecast.client.asyncio.sleep", sleeper)
    with pytest.raises(OzonRateLimitError) as error:
        await client._post("/v1/analytics/stocks", {})
    assert error.value.retry_seconds == 120
    assert calls == ["/v1/analytics/stocks"]
    sleeper.assert_not_awaited()
    await client.close()


def test_retry_after_formats():
    from datetime import UTC, datetime, timedelta
    from email.utils import format_datetime

    assert retry_after_seconds("120") == 120
    assert retry_after_seconds(None) == 60
    assert retry_after_seconds("invalid") == 60
    assert retry_after_seconds("-10") == 60
    future = format_datetime(datetime.now(UTC) + timedelta(seconds=120), usegmt=True)
    assert 119 <= retry_after_seconds(future) <= 120


def test_rate_limit_cooldown_and_retry_header(tmp_path, monkeypatch):
    service, client = make_service(tmp_path)
    clock = [1000.0]
    monkeypatch.setattr("ozon_forecast.service.time.monotonic", lambda: clock[0])
    client.get_catalog.side_effect = OzonRateLimitError(120)
    with TestClient(create_app(service)) as http:
        first = http.get("/api/v1/forecast")
        assert first.status_code == 503
        assert first.headers["retry-after"] == "120"
        clock[0] += 30
        second = http.get("/api/v1/forecast")
        assert second.headers["retry-after"] == "90"
        client.get_catalog.assert_awaited_once()
        clock[0] += 91
        http.get("/api/v1/forecast")
        assert client.get_catalog.await_count == 2


def test_rate_limit_warning_uses_effective_minimum_cooldown(tmp_path):
    service, client = make_service(tmp_path)
    client.get_catalog.side_effect = OzonRateLimitError(1)
    with TestClient(create_app(service)) as http:
        result = http.get("/api/v1/forecast")
        assert result.status_code == 503
        assert result.headers["retry-after"] == "30"
        assert "через 30 сек" in result.json()["detail"]


def test_http_client_url_logging_disabled(tmp_path):
    import logging

    service, _ = make_service(tmp_path)
    with TestClient(create_app(service)):
        assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)
        assert not logging.getLogger("httpcore").isEnabledFor(logging.DEBUG)


@pytest.mark.asyncio
async def test_rate_limit_preserves_stale_snapshot_without_new_calls(tmp_path):
    service, client = make_service(tmp_path)
    fresh = await service.report(30)
    service._loaded_at = 0
    client.get_inventory.side_effect = OzonRateLimitError(120)
    stale = await service.report(30)
    repeated = await service.report(60)
    assert stale.stale and repeated.stale
    assert stale.fetched_at == fresh.fetched_at == repeated.fetched_at
    assert "429" in stale.warning
    assert client.get_inventory.await_count == 2
