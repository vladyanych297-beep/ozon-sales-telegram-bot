from datetime import date, datetime
from unittest.mock import AsyncMock

import pytest

from ozon_forecast.models import Product
from ozon_forecast.order_income import cash_day
from ozon_forecast.profitability import CostInput, ProfitabilityService
from ozon_forecast.settings import ServerSettings


def m(n):
    return {"amount": str(n), "currency": "RUB"}


def sale(number="chosen", fee=-100, price=300, bonus=195, partner=5):
    return {
        "accrual_id": number,
        "unit_number": number,
        "accrued_category": "POSTING",
        "posting": {
            "products": [
                {
                    "sku": 10,
                    "quantity": 1,
                    "commission": {
                        "seller_price": m(500),
                        "sale_price": m(price),
                        "bonus": m(bonus),
                        "coinvestment": m(partner),
                        "commission": m(fee),
                    },
                    "delivery": None,
                }
            ]
        },
    }


def test_components_returns_refusal_and_exact_order_links():
    normal = sale()
    reverse = sale(fee=100)
    delivery = {
        "unit_number": "chosen",
        "posting": {
            "products": [
                {
                    "sku": 10,
                    "quantity": 1,
                    "commission": None,
                    "delivery": {"total_accrued": m(-40), "services": [{"accrued": m(-40)}]},
                }
            ]
        },
    }
    acquiring = {
        "unit_number": "chosen",
        "item_fees": {"fees": [{"sku": 10, "fees": [{"accrued": m(-7)}]}]},
    }
    v, count = cash_day(
        [normal, reverse, delivery, acquiring, sale("outside")], {"chosen": {"10": 1}}
    )
    assert count == 0
    assert v["10"]["revenue_cents"] == 0
    assert (v["10"]["redeemed"], v["10"]["returned"], v["10"]["fees_cents"]) == (1, 1, 4700)
    assert cash_day([delivery], {"chosen": {"10": 1}})[0]["10"]["redeemed"] == 0
    assert cash_day([normal], {"chosen": {"10": 1}})[0]["10"]["revenue_cents"] == 50000


@pytest.mark.asyncio
async def test_cohort_uses_later_accruals_but_not_later_common_expenses(tmp_path, monkeypatch):
    import ozon_forecast.order_income as module

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 12, 12, tzinfo=tz)

    monkeypatch.setattr(module, "datetime", Frozen)
    client = AsyncMock()
    client.get_catalog.return_value = [Product("1", "offer", "product", frozenset({"10"}))]

    async def postings(endpoint, body):
        return {
            "postings": [
                {
                    "posting_number": "chosen",
                    "status": "delivered",
                    "products": [{"sku": 10, "quantity": 1}],
                }
            ]
            if "/fbo/" in endpoint
            else [],
            "has_next": False,
        }

    client._post.side_effect = postings
    service = ProfitabilityService(
        ServerSettings("c", "k", snapshot_path=tmp_path / "snapshot.json"), client
    )
    service.store.save(CostInput(product_id="1", cost_cents=10000))
    common = {"accrual_id": "common", "accrued_category": "NON_ITEM", "total_amount": m(-10)}

    async def finance(endpoint, body):
        if endpoint.endswith("/types"):
            return {"accrual_types": []}
        entries = (
            [common]
            if body["date"] == "2026-11-10"
            else [sale(), sale("outside")]
            if body["date"] == "2026-11-11"
            else [dict(common, accrual_id="later")]
        )
        return {"accruals": entries, "last_id": ""}

    service.finance_post = AsyncMock(side_effect=finance)
    result = await service.report(date(2026, 11, 10), date(2026, 11, 10), True)
    assert result["accounting_basis"] == "order_cash_v2"
    assert result["accrual_date_to"] == "2026-11-12"
    assert result["order_count"] == 1
    assert result["rows"][0]["ordered"] == 1
    assert result["rows"][0]["revenue_cents"] == 50000
    assert result["rows"][0]["profit_cents"] == 30000
    assert result["unbound_expenses_cents"] == 1000
    saved = await service.report(date(2026, 11, 10), date(2026, 11, 10), False)
    assert saved["stale"] and saved["rows"] == result["rows"]
    await service.report(date(2026, 11, 10), date(2026, 11, 10), True)
    assert service.finance_post.await_count == 8  # latest 14 days always fresh


@pytest.mark.asyncio
async def test_fast_cache_full_reconciliation_and_expiry(tmp_path, monkeypatch):
    import ozon_forecast.order_income as module

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 12, 12, tzinfo=tz)

    monkeypatch.setattr(module, "datetime", Frozen)
    client = AsyncMock()
    client.get_catalog.return_value = []
    client._post.return_value = {"postings": [], "has_next": False}
    service = ProfitabilityService(
        ServerSettings("c", "k", snapshot_path=tmp_path / "s.json"), client
    )

    async def finance(endpoint, body):
        return (
            {"accrual_types": []}
            if endpoint.endswith("/types")
            else {"accruals": [], "last_id": ""}
        )

    service.finance_post = AsyncMock(side_effect=finance)
    a = b = date(2026, 10, 20)
    await service.report(a, b, True)
    assert service.finance_post.await_count == 25
    service.finance_post.reset_mock()
    await service.report(a, b, True)
    assert service.finance_post.await_count == 15
    service.finance_post.reset_mock()
    await service.report(a, b, True, full_reload=True)
    assert service.finance_post.await_count == 25
    with service.store.connect() as db:
        db.execute("UPDATE order_cash_days_v2 SET fetched_at='2026-11-11T11:59:00+03:00'")
    service.finance_post.reset_mock()
    await service.report(a, b, True)
    assert service.finance_post.await_count == 25
