"""Accrual-date income components; never add points on top of seller_price."""
from .profitability import rubles
from ozon_sales_bot.ozon import OzonApiError


def income_components(commission, quantity):
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
        raise OzonApiError("Ozon: некорректное количество финансовой операции")
    # All prices are per unit. Preserve signs on refunds; points are a
    # separate component, not a second addition to the nominal seller price.
    buyer = rubles(commission.get("sale_price")) * quantity
    points = rubles(commission.get("bonus")) * quantity
    subsidy = rubles(commission.get("coinvestment")) * quantity
    return dict(buyer_paid_cents=buyer, discount_points_cents=points,
                ozon_subsidy_cents=subsidy,
                revenue_cents=buyer + points + subsidy)


def accrual_day_rows(entries):
    """Normalize only explicit SKU links. Unallocated amounts stay separate."""
    rows = {}
    general = 0
    def row(sku):
        return rows.setdefault(str(sku), dict(revenue_cents=0, buyer_paid_cents=0,
            discount_points_cents=0, ozon_subsidy_cents=0, fees_cents=0,
            redeemed=0, returned=0))
    for entry in entries:
        total = rubles(entry.get("total_amount"))
        allocated = 0
        posting = entry.get("posting")
        if posting:
            for product in posting.get("products", []):
                sku = product.get("sku")
                if not sku:
                    raise OzonApiError("Ozon: финансовая операция без SKU")
                value = row(sku)
                commission = product.get("commission")
                if commission:
                    quantity = product.get("quantity")
                    parts = income_components(commission, quantity)
                    # Negative sales prices identify reversals even with zero fee.
                    is_return = parts["revenue_cents"] < 0 or rubles(commission.get("sale_commission")) > 0
                    if is_return:
                        parts = {name:-abs(amount) for name,amount in parts.items()}
                    signed_quantity = -quantity if is_return else quantity
                    value["redeemed" if signed_quantity >= 0 else "returned"] += abs(signed_quantity)
                    for name, amount in parts.items():
                        value[name] += amount
                    fee = -rubles(commission.get("sale_commission"))
                    value["fees_cents"] += fee
                    allocated += parts["revenue_cents"] - fee
                delivery = product.get("delivery")
                if delivery:
                    fee = -rubles(delivery.get("total_accrued"))
                    value["fees_cents"] += fee
                    allocated -= fee
        item_fees = entry.get("item_fees")
        if item_fees and entry.get("unit_number"):
            for item in item_fees.get("fees", []):
                sku = item.get("sku")
                if not sku:
                    raise OzonApiError("Ozon: удержание без SKU")
                fee = sum(-rubles(x.get("accrued")) for x in item.get("fees", []))
                row(sku)["fees_cents"] += fee
                allocated -= fee
        # Exact reconciliation: never distribute unexplained differences to SKUs.
        general += allocated - total
    return rows, general


async def accrual_report(service, date_from, date_to, refresh):
    import json
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    from decimal import Decimal, ROUND_HALF_UP
    now = datetime.now(ZoneInfo(service.settings.timezone))
    if date_from > date_to or date_to > now.date() or (date_to-date_from).days >= 365:
        raise ValueError("Выберите период от 1 до 365 дней, не позднее сегодня")
    key = f"accrual-v1:{date_from}/{date_to}"
    def render(data, stale=False, warning=None):
        costs = service.store.all()
        result = json.loads(json.dumps(data))
        for row in result["rows"]:
            cost = costs.get(row["product_id"])
            profit = None if cost is None else row["revenue_cents"] - row["fees_cents"] - cost*(row["redeemed"]-row["returned"])
            row.update(cost_cents=cost, profit_cents=profit,
                       margin_percent=None if profit is None or row["revenue_cents"] <= 0 else str((Decimal(profit)*100/row["revenue_cents"]).quantize(Decimal("0.01"),rounding=ROUND_HALF_UP)))
        result.update(stale=stale, warning=warning)
        return result
    async with service.lock:
        cached = service.cached(key)
        if not refresh:
            return render(cached, True, "Сохранённый отчёт по датам начислений") if cached else None
        with service.store.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS accrual_days_v1 (day TEXT PRIMARY KEY, fetched_at TEXT NOT NULL, payload TEXT NOT NULL)")
        try:
            service.progress = "Получаем каталог…"
            products = await service.client.get_catalog()
            by_sku = {str(sku):p for p in products for sku in p.skus}
            accumulated = {}
            general = 0
            day = date_from
            while day <= date_to:
                service.progress = f"Начисления и списания: день {(day-date_from).days+1} из {(date_to-date_from).days+1}"
                with service.store.connect() as db:
                    saved = db.execute("SELECT fetched_at,payload FROM accrual_days_v1 WHERE day=?",(str(day),)).fetchone()
                age = (now.date()-day).days
                ttl = 300 if age == 0 else 3600 if age <= 7 else 86400
                if saved and 0 <= (now-datetime.fromisoformat(saved[0])).total_seconds() < ttl:
                    entries = json.loads(saved[1])
                else:
                    entries=[]; seen=set(); cursor=""
                    while True:
                        payload=await service.finance_post("/v1/finance/accrual/by-day",dict(date=str(day),last_id=cursor))
                        page=payload.get("accruals"); next_cursor=payload.get("last_id")
                        if not isinstance(page,list) or not isinstance(next_cursor,str):
                            raise OzonApiError("Ozon: неполный ответ финансовых операций")
                        added=0
                        for item in page:
                            identity=str(item.get("accrual_id") or "")
                            if not identity:raise OzonApiError("Ozon: нет идентификатора операции")
                            if identity not in seen:
                                entries.append(item);seen.add(identity);added+=1
                        if not next_cursor:break
                        if next_cursor == cursor or not added:raise OzonApiError("Ozon: повторная страница операций")
                        cursor=next_cursor
                    # Validate before marking the day complete.
                    accrual_day_rows(entries)
                    with service.store.connect() as db:
                        db.execute("INSERT INTO accrual_days_v1 VALUES (?,?,?) ON CONFLICT(day) DO UPDATE SET fetched_at=excluded.fetched_at,payload=excluded.payload",(str(day),datetime.now(ZoneInfo(service.settings.timezone)).isoformat(),json.dumps(entries,ensure_ascii=False)))
                rows, extra=accrual_day_rows(entries)
                general+=extra
                for sku, values in rows.items():
                    if sku not in by_sku:raise OzonApiError("Ozon: финансовый SKU отсутствует в каталоге")
                    product=by_sku[sku]
                    target=accumulated.setdefault(product.product_id,dict(product_id=product.product_id,offer_id=product.offer_id,name=product.name,**{k:0 for k in values}))
                    for field,amount in values.items():target[field]+=amount
                day+=timedelta(days=1)
            rows=list(accumulated.values())
            for row in rows:
                # In an accrual-date report this is the number of sale accrual units,
                # not orders placed during the period. Returns may exceed sales.
                row["ordered"]=row["redeemed"]
                row["average_fee_cents"]=None if not row["ordered"] else int((Decimal(row["fees_cents"])/row["ordered"]).quantize(Decimal(1),rounding=ROUND_HALF_UP))
            data=dict(accounting_basis="accrual_date_v1",date_from=str(date_from),date_to=str(date_to),timezone=service.settings.timezone,
                fetched_at=datetime.now(ZoneInfo(service.settings.timezone)).isoformat(),rows=sorted(rows,key=lambda r:(-r["revenue_cents"],r["name"])),
                expenses_complete=True,unbound_expenses_cents=general,
                unbound_expenses=[dict(name="Общие операции и нераспределённые корректировки Ozon",amount_cents=general)] if general else [],excluded_accruals=0)
            with service.store.connect() as db:
                db.execute("INSERT INTO reports VALUES (?,?) ON CONFLICT(period) DO UPDATE SET payload=excluded.payload",(key,json.dumps(data,ensure_ascii=False)))
            return render(data)
        except OzonApiError as exc:
            if cached:return render(cached,True,str(exc))
            raise
