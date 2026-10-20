from datetime import date
from unittest.mock import AsyncMock

import pytest
from ozon_forecast.models import Product
from ozon_forecast.profitability import (CostStore, CostInput, OrderAccrual,
    profitability_rows, parse_accruals, ProfitabilityService)
from ozon_forecast.settings import ServerSettings
from ozon_sales_bot.ozon import OzonApiError


def money(amount):
    return {"amount": str(amount), "currency": "RUB"}


def test_strict_attribution_and_refund():
    entries = [
        dict(sku=10, type_id=69, quantity=2, seller_price=money(500), accrued=money(-100)),
        dict(sku=10, type_id=69, quantity=1, seller_price=money(500), accrued=money(50)),
        dict(sku=10, type_id=32, accrued=money(-70)),
        dict(sku=None, type_id=32, accrued=money(-999)),
        dict(sku=11, type_id=32, accrued=money(-999)),
    ]
    accruals, excluded = parse_accruals({"posting_accruals": [dict(posting_number="a", accruals=entries)]}, {"a": {"10": 2}}, 69)
    assert excluded == 2
    p = Product("1", "offer", "name", frozenset({"10"}))
    row = profitability_rows([p], {"10": 2}, accruals, {"1": 20000})[0]
    assert (row["ordered"], row["redeemed"], row["returned"]) == (2, 2, 1)
    assert row["revenue_cents"] == 50000
    assert row["fees_cents"] == 12000
    assert row["average_fee_cents"] == 6000
    assert row["profit_cents"] == 18000
    assert row["margin_percent"] == "36.00"
    assert profitability_rows([p], {"10": 2}, accruals, {})[0]["profit_cents"] is None


def test_no_unknown_or_duplicate_postings():
    for groups in [[dict(posting_number="other", accruals=[])], [dict(posting_number="a", accruals=[])] * 2]:
        with pytest.raises(OzonApiError):
            parse_accruals({"posting_accruals": groups}, {"a": {"10": 1}}, 69)


def test_shared_cost_persists(tmp_path):
    path = tmp_path / "cost.sqlite3"
    CostStore(path).save(CostInput(product_id="1", cost_cents=12345))
    other = CostStore(path)
    assert other.all() == {"1": 12345}
    other.save(CostInput(product_id="1", cost_cents=0))
    assert CostStore(path).all() == {"1": 0}


@pytest.mark.asyncio
async def test_report_cache_survives_failure_and_cost_changes(tmp_path):
    settings = ServerSettings("client", "key", snapshot_path=tmp_path / "forecast.json")
    client = AsyncMock()
    client.get_catalog.return_value = [Product("1", "offer", "name", frozenset({"10"}))]
    client._get_fbo_postings.return_value = [dict(posting_number="a", products=[dict(sku=10, quantity=1)])]
    client._get_fbs_postings.return_value = []
    client._post.side_effect = [{"accrual_types": [dict(id=69, name="SaleCommission")]},
        {"posting_accruals": [dict(posting_number="a", accruals=[dict(sku=10, type_id=69, quantity=1, seller_price=money(500), accrued=money(-50))])]}]
    service = ProfitabilityService(settings, client)
    result = await service.report(date(2026, 1, 1), date(2026, 1, 2), True)
    assert result["stale"] is False
    service = ProfitabilityService(settings, client)
    service.store.save(CostInput(product_id="1", cost_cents=20000))
    client.get_catalog.side_effect = OzonApiError("unavailable")
    cached = await service.report(date(2026, 1, 1), date(2026, 1, 2), True)
    assert cached["stale"] is True
    assert cached["rows"][0]["profit_cents"] == 25000
    assert cached["fetched_at"] == result["fetched_at"]


@pytest.mark.parametrize("entry", [
    dict(sku=10, type_id=32, accrued={"amount": "1", "currency": "USD"}),
    dict(sku=10, type_id=32, accrued={"amount": "NaN", "currency": "RUB"}),

])
def test_ambiguous_financial_values_fail(entry):
    with pytest.raises(OzonApiError):
        parse_accruals({"posting_accruals": [dict(posting_number="a", accruals=[entry])]}, {"a": {"10": 1}}, 69)


def test_api_shared_cost_and_validation(tmp_path):
    from fastapi.testclient import TestClient
    from types import SimpleNamespace
    from ozon_forecast.api import create_app
    service = SimpleNamespace(settings=ServerSettings("client", "key", snapshot_path=tmp_path / "forecast.json"), client=AsyncMock())
    with TestClient(create_app(service)) as api:
        response = api.post("/api/v1/profitability/cost", json={"product_id": "1", "cost_cents": 12345})
        assert response.status_code == 200
        assert CostStore(tmp_path / "profitability.sqlite3").all() == {"1": 12345}
        assert api.post("/api/v1/profitability/cost", json={"product_id": "1", "cost_cents": True}).status_code == 422
        assert api.get("/api/v1/profitability?date_from=2026-01-02&date_to=2026-01-01").status_code == 422
        assert api.get("/api/v1/profitability?date_from=2026-01-01&date_to=2026-01-02").status_code == 404
        service.client._post.assert_not_awaited()


def test_zero_commission_and_negative_refund_price():
    entries = [dict(sku=10, type_id=69, quantity=1, seller_price=money(500), accrued=money(0)),
               dict(sku=10, type_id=69, quantity=1, seller_price=money(-500), accrued=money(0))]
    rows, excluded = parse_accruals({"posting_accruals": [dict(posting_number="a", accruals=entries)]}, {"a": {"10": 1}}, 69)
    assert (rows[0].redeemed, rows[0].revenue_cents) == (1, 50000)
    assert (rows[1].returned, rows[1].revenue_cents) == (1, -50000)


@pytest.mark.asyncio
async def test_partial_batches_resume_without_reloading_orders(tmp_path):
    settings = ServerSettings("client", "key", snapshot_path=tmp_path / "forecast.json")
    client = AsyncMock()
    client.get_catalog.return_value = [Product("1", "offer", "name", frozenset({"10"}))]
    client._get_fbo_postings.return_value = [dict(posting_number=str(i), products=[dict(sku=10, quantity=1)]) for i in range(201)]
    client._get_fbs_postings.return_value = []
    types = {"accrual_types": [dict(id=69, name="SaleCommission")]}
    def batch(numbers):
        return {"posting_accruals": [dict(posting_number=str(i), accruals=[dict(sku=10, type_id=69, quantity=1, seller_price=money(500), accrued=money(-50))]) for i in numbers]}
    service = ProfitabilityService(settings, client)
    service.finance_post = AsyncMock(side_effect=[types, batch(range(200)), OzonApiError("limited")])
    with pytest.raises(OzonApiError):
        await service.report(date(2026, 1, 1), date(2026, 1, 2), True)
    resumed = ProfitabilityService(settings, client)
    resumed.finance_post = AsyncMock(side_effect=[types, batch([200])])
    result = await resumed.report(date(2026, 1, 1), date(2026, 1, 2), True)
    assert result["rows"][0]["redeemed"] == 201
    client.get_catalog.assert_awaited_once()
    assert resumed.finance_post.call_args.args[1] == {"posting_numbers": ["200"]}


@pytest.mark.asyncio
async def test_financial_rate_limit_retries_same_request(tmp_path, monkeypatch):
    from ozon_forecast.client import OzonRateLimitError
    import ozon_forecast.profitability as module
    sleep = AsyncMock()
    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    client = AsyncMock()
    client._post.side_effect = [OzonRateLimitError(1), OzonRateLimitError(1), {"ok": True}]
    service = ProfitabilityService(ServerSettings("c", "k", snapshot_path=tmp_path / "forecast.json"), client)
    assert await service.finance_post("/finance", {"posting_numbers": ["a"]}) == {"ok": True}
    assert client._post.await_count == 3
    assert 2 in [call.args[0] for call in sleep.call_args_list]
    assert 4 in [call.args[0] for call in sleep.call_args_list]
