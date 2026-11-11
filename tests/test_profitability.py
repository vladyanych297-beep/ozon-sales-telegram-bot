from datetime import date
from unittest.mock import AsyncMock

import pytest
from ozon_forecast.models import Product
from ozon_forecast.profitability import (CostStore, CostInput, OrderAccrual,
    profitability_rows, parse_accruals, ProfitabilityService, unbound_expenses)
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
        {"posting_accruals": [dict(posting_number="a", accruals=[dict(sku=10, type_id=69, quantity=1, seller_price=money(500), accrued=money(-50))])]}, {"accruals": [], "last_id": ""}, {"accruals": [], "last_id": ""}]
    service = ProfitabilityService(settings, client)
    result = await service.legacy_order_report(date(2026, 1, 1), date(2026, 1, 2), True)
    assert result["stale"] is False
    service = ProfitabilityService(settings, client)
    service.store.save(CostInput(product_id="1", cost_cents=20000))
    client.get_catalog.side_effect = OzonApiError("unavailable")
    cached = await service.legacy_order_report(date(2026, 1, 1), date(2026, 1, 2), True)
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
        await service.legacy_order_report(date(2026, 1, 1), date(2026, 1, 2), True)
    resumed = ProfitabilityService(settings, client)
    resumed.finance_post = AsyncMock(side_effect=[types, batch([200]), {"accruals": [], "last_id": ""}, {"accruals": [], "last_id": ""}])
    result = await resumed.legacy_order_report(date(2026, 1, 1), date(2026, 1, 2), True)
    assert result["rows"][0]["redeemed"] == 201
    client.get_catalog.assert_awaited_once()
    assert resumed.finance_post.call_args_list[1].args[1] == {"posting_numbers": ["200"]}


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


def test_unbound_expenses_exclude_posting_fees_and_net_refunds():
    entries = [
        dict(accrued_category="ITEM", unit_number="order", posting=None, total_amount=money(-99)),
        dict(accrued_category="POSTING", posting={}, total_amount=money(-99)),
        dict(accrued_category="NON_ITEM", non_item_fee=dict(type_id=5, accrued=money(-100)), total_amount=money(-100)),
        dict(accrued_category="NON_ITEM", non_item_fee=dict(type_id=5, accrued=money(20)), total_amount=money(20)),
        dict(accrued_category="CONTAINER", container_fees=None, total_amount=money(-7)),
    ]
    result = unbound_expenses(entries, {"5": "Advertising"})
    assert result["Advertising"] == 8000
    assert sum(result.values()) == 8700
    with pytest.raises(OzonApiError):
        unbound_expenses([dict(accrued_category="UNKNOWN")], {})


@pytest.mark.asyncio
async def test_expense_pages_resume_and_persist(tmp_path):
    from datetime import timedelta
    settings = ServerSettings("c", "k", snapshot_path=tmp_path / "forecast.json")
    client = AsyncMock()
    client.get_catalog.return_value = []
    client._get_fbo_postings.return_value = []
    client._get_fbs_postings.return_value = []
    types = {"accrual_types": [dict(id=69, name="SaleCommission"), dict(id=5, name="Advertising", description="Advertising")]}
    fee = dict(accrual_id=1, accrued_category="NON_ITEM", total_amount=money(-100), non_item_fee=dict(type_id=5, accrued=money(-100)))
    service = ProfitabilityService(settings, client)
    service.finance_post = AsyncMock(side_effect=[types, dict(accruals=[fee], last_id="next"), OzonApiError("limited")])
    day = date(2026, 9, 30)
    with pytest.raises(OzonApiError):
        await service.legacy_order_report(day, day, True)
    resumed = ProfitabilityService(settings, client)
    credit = dict(accrual_id=2, accrued_category="NON_ITEM", total_amount=money(20), non_item_fee=dict(type_id=5, accrued=money(20)))
    resumed.finance_post = AsyncMock(side_effect=[types, dict(accruals=[fee, credit], last_id="")])
    result = await resumed.legacy_order_report(day, day, True)
    assert result["expenses_complete"] is True
    assert result["unbound_expenses_cents"] == 8000
    assert result["unbound_expenses"] == [dict(name="Advertising", amount_cents=8000)]
    assert resumed.finance_post.call_args.args[1] == dict(date=str(day), last_id="next")
    client.get_catalog.assert_awaited_once()
    cached = await ProfitabilityService(settings, client).legacy_order_report(day, day, False)
    assert cached["unbound_expenses_cents"] == 8000
    assert cached["stale"] is True


def test_background_report_short_requests_and_deduplicated_start(tmp_path):
    import asyncio
    import time
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    from ozon_forecast.api import create_app
    source = SimpleNamespace(settings=ServerSettings("c", "k", snapshot_path=tmp_path / "forecast.json"), client=AsyncMock())
    app = create_app(source)
    service = ProfitabilityService(source.settings, source.client)
    async def slow(*args):
        await asyncio.sleep(0.15)
        return {"rows": [], "expenses_complete": True}
    service.report = AsyncMock(side_effect=slow)
    app.state.profitability = service
    with TestClient(app) as client:
        url = "/api/v1/profitability/jobs?date_from=2026-01-01&date_to=2026-01-02"
        job = client.post(url).json()["job_id"]
        assert client.post(url).json()["job_id"] == job
        assert client.get(f"/api/v1/profitability/jobs/{job}").json()["status"] == "running"
        assert client.post(url.replace("01-02", "01-03")).status_code == 409
        for _ in range(30):
            result = client.get(f"/api/v1/profitability/jobs/{job}").json()
            if result["status"] == "complete":
                break
            time.sleep(0.02)
        assert result["report"]["expenses_complete"] is True
        service.report.assert_awaited_once()
        assert client.get("/api/v1/profitability/jobs/missing").status_code == 404


@pytest.mark.asyncio
async def test_expense_day_cache_reused_by_smaller_period_after_restart(tmp_path):
    settings = ServerSettings("c", "k", snapshot_path=tmp_path / "forecast.json")
    client = AsyncMock()
    client.get_catalog.return_value = []
    client._get_fbo_postings.return_value = []
    client._get_fbs_postings.return_value = []
    types = {"accrual_types": [dict(id=69, name="SaleCommission")]}
    service = ProfitabilityService(settings, client)
    fee = dict(accrual_id=1, accrued_category="NON_ITEM", total_amount=money(-100))
    service.finance_post = AsyncMock(side_effect=[types, dict(accruals=[fee], last_id=""), dict(accruals=[], last_id="")])
    await service.legacy_order_report(date(2026, 1, 1), date(2026, 1, 2), True)
    restarted = ProfitabilityService(settings, client)
    restarted.finance_post = AsyncMock(return_value=types)
    result = await restarted.legacy_order_report(date(2026, 1, 1), date(2026, 1, 1), True)
    assert result["unbound_expenses_cents"] == 10000
    assert restarted.finance_post.await_count == 1
    assert restarted.finance_post.call_args.args[0] == "/v1/finance/accrual/types"
    with restarted.store.connect() as db:
        db.execute("UPDATE expense_days SET fetched_at='2000-01-01T00:00:00+00:00'")
    refreshed = ProfitabilityService(settings, client)
    refreshed.finance_post = AsyncMock(side_effect=[types, dict(accruals=[], last_id="")])
    result = await refreshed.legacy_order_report(date(2026, 1, 1), date(2026, 1, 1), True)
    assert result["unbound_expenses_cents"] == 0
    assert refreshed.finance_post.await_count == 2


def test_accrual_income_components_do_not_double_count_seller_price():
    from ozon_forecast.accrual_income import income_components
    commission = dict(seller_price=money(700), sale_price=money("400.95"),
                      bonus=money("295.04"), coinvestment=money("4.01"))
    result = income_components(commission, 2)
    assert result == dict(buyer_paid_cents=80190, discount_points_cents=59008,
                          ozon_subsidy_cents=802, revenue_cents=140000)
    with pytest.raises(OzonApiError):
        income_components(dict(seller_price=money(700)), 1)


def test_daily_accrual_income_reconciles_without_double_counting():
    from ozon_forecast.accrual_income import accrual_day_rows
    sale = dict(total_amount=money("257.20"), posting=dict(products=[dict(sku=10, quantity=1,
        commission=dict(sale_price=money("400.95"), bonus=money("295.04"),
            coinvestment=money("4.01"), sale_commission=money(-329)),
        delivery=dict(total_accrued=money("-113.80")))]))
    fee = dict(unit_number="posting",total_amount=money(-10), item_fees=dict(fees=[dict(sku=10, fees=[dict(accrued=money(-10))])]))
    common = dict(total_amount=money(-50))
    rows, general = accrual_day_rows([sale, fee, common])
    assert rows["10"]["revenue_cents"] == 70000
    assert rows["10"]["fees_cents"] == 45280
    assert general == 5000
    assert rows["10"]["revenue_cents"] - rows["10"]["fees_cents"] - general == 19720


@pytest.mark.asyncio
async def test_accrual_date_report_includes_components_and_reuses_days(tmp_path):
    settings = ServerSettings("c", "k", snapshot_path=tmp_path / "forecast.json")
    client = AsyncMock()
    client.get_catalog.return_value = [Product("1", "offer", "name", frozenset({"10"}))]
    entry = dict(accrual_id=1, total_amount=money(400), posting=dict(products=[dict(sku=10,quantity=1,
        commission=dict(sale_price=money(300),bonus=money(195),coinvestment=money(5),sale_commission=money(-100)))]))
    service = ProfitabilityService(settings, client)
    service.store.save(CostInput(product_id="1",cost_cents=10000))
    service.finance_post = AsyncMock(side_effect=[dict(accruals=[entry],last_id=""),dict(accruals=[],last_id="")])
    result = await service.report(date(2026,1,1),date(2026,1,2),True)
    assert result["accounting_basis"] == "accrual_date_v1"
    assert result["rows"][0]["profit_cents"] == 30000
    assert result["rows"][0]["discount_points_cents"] == 19500
    client._get_fbo_postings.assert_not_awaited()
    smaller = await service.report(date(2026,1,1),date(2026,1,1),True)
    assert smaller["rows"][0]["buyer_paid_cents"] == 30000
    assert service.finance_post.await_count == 2
    assert await service.report(date(2025,1,1),date(2025,1,1),False) is None


def test_accrual_refund_without_sale_in_period():
    from ozon_forecast.accrual_income import accrual_day_rows
    rows, common = accrual_day_rows([dict(total_amount=money(-400),posting=dict(products=[dict(sku=10,quantity=1,
        commission=dict(sale_price=money(300),bonus=money(195),coinvestment=money(5),sale_commission=money(100)))]))])
    assert rows["10"]["returned"] == 1
    assert rows["10"]["redeemed"] == 0
    assert rows["10"]["revenue_cents"] == -50000
    assert rows["10"]["fees_cents"] == -10000
    assert common == 0
