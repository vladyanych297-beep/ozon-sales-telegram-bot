"""Order cohorts and daily cash components, matching Marzha Ozon 1.0.9."""

import json
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from ozon_sales_bot.ozon import OzonApiError

from .profitability import rubles, unbound_expenses

BASIS = "order_cash_v2"


def quantities(postings):
    orders, metadata = {}, {}
    for scheme, items in postings:
        for p in items:
            number = p.get("posting_number")
            if not number or number in orders:
                raise OzonApiError("Ozon: повторное или пустое отправление")
            q = {}
            for product in p.get("products", []):
                sku, count = str(product.get("sku") or ""), product.get("quantity")
                if (
                    not sku.isdecimal()
                    or isinstance(count, bool)
                    or not isinstance(count, int)
                    or count <= 0
                ):
                    raise OzonApiError("Ozon: некорректный состав отправления")
                q[sku] = q.get(sku, 0) + count
            orders[number] = q
            metadata[number] = {
                "scheme": scheme,
                "status": p.get("status") or "",
                "created": p.get("created_at") or "",
            }
    return orders, metadata


def cash_day(entries, orders):
    values, excluded = {}, 0

    def row(sku):
        return values.setdefault(
            sku,
            {
                "revenue_cents": 0,
                "buyer_paid_cents": 0,
                "discount_points_cents": 0,
                "ozon_subsidy_cents": 0,
                "fees_cents": 0,
                "redeemed": 0,
                "returned": 0,
            },
        )

    for entry in entries:
        selected = orders.get(entry.get("unit_number"))
        if selected is None:
            continue
        for product in (entry.get("posting") or {}).get("products", []):
            sku = str(product.get("sku") or "")
            if sku not in selected:
                excluded += 1
                continue
            q = product.get("quantity")
            if isinstance(q, bool) or not isinstance(q, int) or q <= 0:
                raise OzonApiError("Ozon: нет количества финансовой операции")
            v = row(sku)
            if "commission" not in product or "delivery" not in product:
                raise OzonApiError("Ozon: неполные сведения о продаже или доставке")
            c = product["commission"]
            if c is not None:
                price = rubles(c.get("sale_price"))
                direction = (
                    rubles(c["seller_price"]) if c.get("seller_price") is not None else price
                )
                fee = rubles(c.get("commission"))
                sale = fee <= 0 and direction >= 0 and price >= 0
                for field, name in (
                    ("sale_price", "buyer_paid_cents"),
                    ("bonus", "discount_points_cents"),
                    ("coinvestment", "ozon_subsidy_cents"),
                ):
                    amount = rubles(c.get(field))
                    amount = (amount if sale else -abs(amount)) * q
                    v[name] += amount
                    v["revenue_cents"] += amount
                v["fees_cents"] -= fee
                v["redeemed" if sale else "returned"] += q
            delivery = product["delivery"]
            if delivery is not None:
                amount = rubles(delivery.get("total_accrued"))
                services = sum(rubles(s.get("accrued")) for s in delivery.get("services") or [])
                if amount != services:
                    raise OzonApiError("Ozon: детализация доставки не совпала с итогом")
                v["fees_cents"] -= amount
        for group in (entry.get("item_fees") or {}).get("fees", []):
            sku = str(group.get("sku") or "")
            if sku not in selected:
                excluded += len(group.get("fees", []))
                continue
            row(sku)["fees_cents"] -= sum(rubles(f.get("accrued")) for f in group.get("fees", []))
    return values, excluded


async def order_report(service, date_from, date_to, refresh, full_reload=False):
    now = datetime.now(ZoneInfo(service.settings.timezone))
    if date_from > date_to or date_to > now.date() or date_from < now.date() - timedelta(days=364):
        raise ValueError("Выберите заказы за последние 365 дней, не позднее сегодня")
    key = f"{BASIS}:{date_from}/{date_to}"

    def render(data, stale=False, warning=None):
        result = json.loads(json.dumps(data))
        costs = service.store.all()
        for r in result["rows"]:
            cost = costs.get(r["product_id"])
            profit = (
                None
                if cost is None
                else r["revenue_cents"] - r["fees_cents"] - cost * (r["redeemed"] - r["returned"])
            )
            r.update(
                cost_cents=cost,
                profit_cents=profit,
                margin_percent=None
                if profit is None or r["revenue_cents"] <= 0
                else str(
                    (Decimal(profit) * 100 / r["revenue_cents"]).quantize(
                        Decimal(".01"), rounding=ROUND_HALF_UP
                    )
                ),
            )
        result.update(stale=stale, warning=warning)
        return result

    async with service.lock:
        cached = service.cached(key)
        if not refresh:
            return (
                render(cached, True, "Сохранённый отчёт по выбранным заказам") if cached else None
            )
        try:
            service.progress = "Получаем каталог и заказы FBO/FBS…"
            products = await service.client.get_catalog()
            by_sku = {str(s): p for p in products for s in p.skus}
            postings = []
            start = date_from
            while start <= date_to:
                end = min(start + timedelta(days=29), date_to)
                # Explicitly use the same v3/v4 cursor methods as Marzha; no demand/return filters.
                for scheme, endpoint in (
                    ("FBO", "/v3/posting/fbo/list"),
                    ("FBS", "/v4/posting/fbs/list"),
                ):
                    cursor, seen, items = "", set(), []
                    since = datetime.combine(
                        start, datetime.min.time(), tzinfo=ZoneInfo(service.settings.timezone)
                    )
                    until = since.replace(year=end.year, month=end.month, day=end.day) + timedelta(
                        days=1, milliseconds=-1
                    )
                    for _ in range(10000):
                        payload = await service.client._post(
                            endpoint,
                            dict(
                                cursor=cursor,
                                sort_dir="ASC",
                                limit=100,
                                filter={
                                    "since": since.astimezone(UTC)
                                    .isoformat(timespec="milliseconds")
                                    .replace("+00:00", "Z"),
                                    "to": until.astimezone(UTC)
                                    .isoformat(timespec="milliseconds")
                                    .replace("+00:00", "Z"),
                                    "status": [],
                                },
                                translit=False,
                                **{
                                    "with": {
                                        "analytics_data": False,
                                        "financial_data": False,
                                        "legal_info": False,
                                    }
                                },
                            ),
                        )
                        page = payload.get("result", payload)
                        if not isinstance(page.get("postings"), list) or not isinstance(
                            page.get("has_next"), bool
                        ):
                            raise OzonApiError("Ozon: неполный список отправлений")
                        items.extend(page["postings"])
                        if not page["has_next"]:
                            break
                        nxt = page.get("cursor")
                        if not nxt or nxt in seen or not page["postings"]:
                            raise OzonApiError("Ozon: повторная страница отправлений")
                        seen.add(nxt)
                        cursor = nxt
                    else:
                        raise OzonApiError("Ozon: превышен предел страниц отправлений")
                    postings.append((scheme, items))
                start = end + timedelta(days=1)
            orders, _metadata = quantities(postings)
            if any(s not in by_sku for q in orders.values() for s in q):
                raise OzonApiError("Ozon: SKU заказа отсутствует в каталоге")
            types = await service.finance_post("/v1/finance/accrual/types", {})
            names = {
                str(t["id"]): t.get("description") or t["name"] for t in types["accrual_types"]
            }
            with service.store.connect() as db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS order_cash_days_v2 (day TEXT PRIMARY KEY, fetched_at TEXT NOT NULL, payload TEXT NOT NULL)"
                )
            totals, expenses, excluded = {}, {}, 0
            day = date_from
            while day <= now.date():
                service.progress = f"Начисления выбранных заказов: {day} (до {now.date()})"
                with service.store.connect() as db:
                    saved = db.execute(
                        "SELECT fetched_at,payload FROM order_cash_days_v2 WHERE day=?", (str(day),)
                    ).fetchone()
                reusable = (
                    not full_reload
                    and saved
                    and day < now.date() - timedelta(days=13)
                    and 0 <= (now - datetime.fromisoformat(saved[0])).total_seconds() < 86400
                )
                if reusable:
                    entries = json.loads(saved[1])
                else:
                    entries = []
                    cursor = ""
                    seen = set()
                    for _ in range(1000):
                        payload = await service.finance_post(
                            "/v1/finance/accrual/by-day", {"date": str(day), "last_id": cursor}
                        )
                        page, nxt = payload.get("accruals"), payload.get("last_id")
                        if not isinstance(page, list) or not isinstance(nxt, str):
                            raise OzonApiError("Ozon: неполные начисления за день")
                        added = 0
                        for e in page:
                            identity = str(e.get("accrual_id") or "")
                            if not identity:
                                raise OzonApiError("Ozon: нет идентификатора начисления")
                            if identity not in seen:
                                seen.add(identity)
                                entries.append(e)
                                added += 1
                        if not nxt:
                            break
                        if nxt == cursor or not added:
                            raise OzonApiError("Ozon: повторная страница начислений")
                        cursor = nxt
                    else:
                        raise OzonApiError("Ozon: превышен предел страниц начислений")
                values, count = cash_day(entries, orders)
                extra = unbound_expenses(entries, names) if day <= date_to else {}
                if not reusable:
                    with service.store.connect() as db:
                        db.execute(
                            "INSERT INTO order_cash_days_v2 VALUES (?,?,?) ON CONFLICT(day) DO UPDATE SET fetched_at=excluded.fetched_at,payload=excluded.payload",
                            (
                                str(day),
                                datetime.now(ZoneInfo(service.settings.timezone)).isoformat(),
                                json.dumps(entries, ensure_ascii=False),
                            ),
                        )
                        db.execute(
                            "DELETE FROM order_cash_days_v2 WHERE day<?",
                            (str(now.date() - timedelta(days=364)),),
                        )
                excluded += count
                for sku, v in values.items():
                    target = totals.setdefault(sku, {k: 0 for k in v})
                    for k, n in v.items():
                        target[k] += n
                for k, n in extra.items():
                    expenses[k] = expenses.get(k, 0) + n
                day += timedelta(days=1)
            rows = []
            for p in products:
                v = {
                    k: sum(totals.get(str(s), {}).get(k, 0) for s in p.skus)
                    for k in (
                        "revenue_cents",
                        "buyer_paid_cents",
                        "discount_points_cents",
                        "ozon_subsidy_cents",
                        "fees_cents",
                        "redeemed",
                        "returned",
                    )
                }
                ordered = sum(q.get(str(s), 0) for q in orders.values() for s in p.skus)
                if not 0 <= v["returned"] <= v["redeemed"] <= ordered:
                    raise OzonApiError("Ozon: продажи и возвраты не согласованы с заказами")
                rows.append(
                    dict(
                        product_id=p.product_id,
                        offer_id=p.offer_id,
                        name=p.name,
                        ordered=ordered,
                        average_fee_cents=None
                        if not ordered
                        else int(
                            (Decimal(v["fees_cents"]) / ordered).quantize(
                                Decimal(1), rounding=ROUND_HALF_UP
                            )
                        ),
                        **v,
                    )
                )
            data = {
                "accounting_basis": BASIS,
                "date_from": str(date_from),
                "date_to": str(date_to),
                "accrual_date_to": str(now.date()),
                "timezone": service.settings.timezone,
                "fetched_at": datetime.now(ZoneInfo(service.settings.timezone)).isoformat(),
                "rows": sorted(rows, key=lambda r: (-r["revenue_cents"], r["name"])),
                "expenses_complete": True,
                "unbound_expenses_cents": sum(expenses.values()),
                "unbound_expenses": [
                    {"name": k, "amount_cents": v} for k, v in sorted(expenses.items()) if v
                ],
                "excluded_accruals": excluded,
                "order_count": len(orders),
            }
            result = render(data)
            with service.store.connect() as db:
                db.execute(
                    "INSERT INTO reports VALUES (?,?) ON CONFLICT(period) DO UPDATE SET payload=excluded.payload",
                    (key, json.dumps(data, ensure_ascii=False)),
                )
            return result
        except OzonApiError as exc:
            if cached:
                return render(cached, True, str(exc))
            raise
