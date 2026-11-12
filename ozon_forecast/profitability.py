"""Order/SKU profitability. Never allocate unbound marketplace fees."""
from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import date, datetime, timedelta
from time import monotonic
from zoneinfo import ZoneInfo
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

from pydantic import BaseModel, Field

from ozon_sales_bot.ozon import OzonApiError


def money_cents(value) -> int:
    try:
        amount = Decimal(str(value))
        if not amount.is_finite():
            raise ValueError
        return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise OzonApiError("Некорректная денежная сумма Ozon") from exc


class CostInput(BaseModel):
    product_id: str = Field(min_length=1, max_length=80, pattern=r"^[0-9]+$")
    cost_cents: int = Field(strict=True, ge=0, le=100_000_000_000)


class CostStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS costs (product_id TEXT PRIMARY KEY, cents INTEGER NOT NULL)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def all(self) -> dict[str, int]:
        with self.connect() as db:
            return dict(db.execute("SELECT product_id, cents FROM costs"))

    def save(self, value: CostInput):
        with self.connect() as db:
            db.execute("INSERT INTO costs VALUES (?, ?) ON CONFLICT(product_id) DO UPDATE SET cents=excluded.cents",
                       (value.product_id, value.cost_cents))


@dataclass(frozen=True)
class OrderAccrual:
    posting: str
    sku: str
    revenue_cents: int
    fees_cents: int
    redeemed: int = 0
    returned: int = 0


def profitability_rows(products, ordered: dict[str, int], accruals: list[OrderAccrual], costs: dict[str, int]):
    by_sku = {}
    for product in products:
        for sku in product.skus:
            if sku in by_sku and by_sku[sku] != product.product_id:
                raise OzonApiError("SKU принадлежит нескольким товарам")
            by_sku[sku] = product.product_id
    values = {p.product_id: [0, 0, 0, 0, 0] for p in products}
    for sku, count in ordered.items():
        if sku not in by_sku:
            raise OzonApiError("В заказах есть товар вне каталога")
        values[by_sku[sku]][0] += count
    for accrual in accruals:
        if not accrual.posting or not accrual.sku:
            raise OzonApiError("Начисление без привязки к заказу и SKU")
        if accrual.sku not in by_sku:
            raise OzonApiError("В финансовом отчёте есть товар вне каталога")
        row = values[by_sku[accrual.sku]]
        row[1] += accrual.redeemed
        row[2] += accrual.returned
        row[3] += accrual.revenue_cents
        row[4] += accrual.fees_cents
    rows = []
    for p in products:
        ordered_count, redeemed, returned, revenue, fees = values[p.product_id]
        cost = costs.get(p.product_id)
        if returned > redeemed or redeemed > ordered_count:
            raise OzonApiError("Ozon: количества финансовых продаж и возвратов не согласованы с заказами")
        net_units = redeemed - returned
        profit = None if cost is None else revenue - fees - cost * net_units
        rows.append(dict(product_id=p.product_id, offer_id=p.offer_id, name=p.name,
                         ordered=ordered_count, redeemed=redeemed, returned=returned,
                         revenue_cents=revenue, fees_cents=fees, cost_cents=cost,
                         average_fee_cents=None if ordered_count == 0 else money_cents(Decimal(fees) / ordered_count / 100),
                         profit_cents=profit,
                         margin_percent=None if profit is None or revenue <= 0 else str(
                             (Decimal(profit) * 100 / revenue).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))))
    return sorted(rows, key=lambda r: (-r["revenue_cents"], r["name"]))


def rubles(value: dict) -> int:
    if not isinstance(value, dict) or value.get("currency") != "RUB" or "amount" not in value:
        raise OzonApiError("Ozon: нет суммы начисления в рублях")
    return money_cents(value["amount"])


def parse_accruals(payload, orders, sale_type):
    """Only exact posting+SKU links; never split posting-wide fees."""
    groups = payload.get("posting_accruals")
    if not isinstance(groups, list):
        raise OzonApiError("Ozon: отсутствуют начисления по отправлениям")
    result, excluded, seen = [], 0, set()
    for group in groups:
        posting = group.get("posting_number")
        if posting not in orders or posting in seen:
            raise OzonApiError("Ozon: неожиданный или повторный номер отправления")
        seen.add(posting)
        entries = group.get("accruals")
        if not isinstance(entries, list):
            raise OzonApiError("Ozon: отсутствует список начислений")
        for item in entries:
            sku = str(item.get("sku") or "")
            if not sku or sku not in orders[posting]:
                excluded += 1
                continue
            fee = rubles(item.get("accrued"))
            revenue = redeemed = returned = 0
            if item.get("type_id") == sale_type:
                quantity = item.get("quantity")
                if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
                    raise OzonApiError("Ozon: нет количества в начислении комиссии за продажу")
                price = rubles(item.get("seller_price"))
                # SaleCommission debit = sale; credit = reversal of that sale.
                if fee <= 0 and price >= 0:
                    redeemed = quantity
                    revenue = price * quantity
                else:
                    returned = quantity
                    revenue = -abs(price) * quantity
            result.append(OrderAccrual(posting, sku, revenue, -fee, redeemed, returned))
    return result, excluded


def unbound_expenses(entries, names):
    """Day accruals without a posting/SKU link. Positive credits reverse expenses."""
    totals = {}
    for entry in entries:
        category = entry.get("accrued_category")
        # ITEM unit_number is an order number even when posting itself is null.
        # These fees are already obtained by /accrual/postings; never count twice.
        if entry.get("posting") is not None or category == "POSTING":
            continue
        if category == "ITEM" and entry.get("unit_number"):
            continue
        if category not in ("ITEM", "NON_ITEM", "CONTAINER"):
            raise OzonApiError("Ozon: неизвестная категория финансового начисления")
        amount = -rubles(entry.get("total_amount"))
        parts = []
        def visit(value):
            if isinstance(value, dict):
                if "type_id" in value and "accrued" in value:
                    parts.append((str(value["type_id"]), -rubles(value["accrued"])))
                else:
                    for child in value.values():
                        visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)
        for field in ("item_fees", "non_item_fee", "container_fees"):
            visit(entry.get(field))
        if not parts or sum(v for _, v in parts) != amount:
            parts = [("other:" + category, amount)]
        for type_id, value in parts:
            label = names.get(type_id, "Прочие расходы Ozon (" + type_id + ")")
            totals[label] = totals.get(label, 0) + value
    return totals


class ProfitabilityService:
    def __init__(self, settings, client):
        self.settings, self.client = settings, client
        self.store = CostStore(settings.snapshot_path.parent / "profitability.sqlite3")
        with self.store.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS reports (period TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        self.lock = asyncio.Lock()
        self.success_at = {}
        self.retry_at = 0.0
        self.finance_at = 0.0
        self.types = None
        self.progress = "Готовим отчёт…"
        with self.store.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS pending (period TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS expense_days (day TEXT PRIMARY KEY, fetched_at TEXT NOT NULL, payload TEXT NOT NULL)")

    async def finance_post(self, endpoint, body):
        from .client import OzonRateLimitError
        # Financial methods have a separate limit. A 1-second Retry-After is
        # not a reason to discard all previously loaded orders and batches.
        for attempt in range(6):
            delay = 2.0 - (monotonic() - self.finance_at)
            if delay > 0:
                await asyncio.sleep(delay)
            self.finance_at = monotonic()
            try:
                return await self.client._post(endpoint, body)
            except OzonRateLimitError as exc:
                if attempt == 5 or exc.retry_seconds > 60:
                    raise
                await asyncio.sleep(max(exc.retry_seconds, 2 ** (attempt + 1)))
        raise OzonApiError("Не удалось загрузить финансовые начисления")

    def save_pending(self, key, payload):
        with self.store.connect() as db:
            db.execute("INSERT INTO pending VALUES (?,?) ON CONFLICT(period) DO UPDATE SET payload=excluded.payload", (key, json.dumps(payload, ensure_ascii=False)))

    def cached(self, key):
        with self.store.connect() as db:
            row = db.execute("SELECT payload FROM reports WHERE period=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def render(self, data, stale=False, warning=None):
        from .models import Product
        products = [Product(p["product_id"], p["offer_id"], p["name"], frozenset(p["skus"])) for p in data["products"]]
        accruals = [OrderAccrual(**a) for a in data["accruals"]]
        return {k: v for k, v in data.items() if k not in ("products", "ordered", "accruals")} | {
            "rows": profitability_rows(products, data["ordered"], accruals, self.store.all()),
            "stale": stale, "warning": warning,
        }

    async def report(self, date_from: date, date_to: date, refresh: bool, full_reload: bool = False):
        from .order_income import order_report
        return await order_report(self, date_from, date_to, refresh, full_reload)

    async def legacy_order_report(self, date_from: date, date_to: date, refresh: bool):
        from dataclasses import asdict
        today = datetime.now(ZoneInfo(self.settings.timezone)).date()
        if date_from > date_to or date_to > today or (date_to - date_from).days >= 365:
            raise ValueError("Выберите период от 1 до 365 дней, не позднее сегодня")
        key = f"{date_from}/{date_to}"
        async with self.lock:
            cached = self.cached(key)
            if not refresh:
                if cached is None:
                    return None
                return self.render(cached, True, "Последний сохранённый отчёт. Нажмите «Обновить» для загрузки.")
            if cached and cached.get("expenses_complete") and monotonic() - self.success_at.get(key, -1000000) < 300:
                return self.render(cached)
            if monotonic() < self.retry_at:
                if cached:
                    return self.render(cached, True, "Повторное обращение к Ozon временно отложено")
                raise OzonApiError("Повторите загрузку через минуту")
            try:
                from .models import Product
                with self.store.connect() as db:
                    saved = db.execute("SELECT payload FROM pending WHERE period=?", (key,)).fetchone()
                work = json.loads(saved[0]) if saved else None
                now = datetime.now(ZoneInfo(self.settings.timezone))
                if work and (now - datetime.fromisoformat(work["started_at"])).total_seconds() > 900:
                    work = None
                if work is None:
                    self.progress = "Получаем каталог и заказы…"
                    products = await self.client.get_catalog()
                    fbo = await self.client._get_fbo_postings(date_from, date_to)
                    fbs = await self.client._get_fbs_postings(date_from, date_to)
                    orders, ordered = {}, {}
                    for posting in fbo + fbs:
                        number = posting.get("posting_number")
                        if not number or number in orders:
                            raise OzonApiError("Ozon: отсутствует или повторяется номер отправления")
                        quantities = {}
                        for product in posting.get("products", []):
                            sku, count = str(product.get("sku") or ""), product.get("quantity")
                            if not sku or isinstance(count, bool) or not isinstance(count, int) or count <= 0:
                                raise OzonApiError("Ozon: некорректный состав заказа")
                            quantities[sku] = quantities.get(sku, 0) + count
                            ordered[sku] = ordered.get(sku, 0) + count
                        orders[number] = quantities
                    work = dict(started_at=now.isoformat(), orders=orders, ordered=ordered,
                                products=[dict(product_id=p.product_id, offer_id=p.offer_id, name=p.name, skus=sorted(p.skus)) for p in products],
                                accruals=[], excluded=0, offset=0)
                    self.save_pending(key, work)
                products = [Product(p["product_id"], p["offer_id"], p["name"], frozenset(p["skus"])) for p in work["products"]]
                orders, ordered = work["orders"], work["ordered"]
                if self.types is None:
                    self.types = await self.finance_post("/v1/finance/accrual/types", {})
                sale_types = [t["id"] for t in self.types.get("accrual_types", []) if t.get("name") == "SaleCommission"]
                if len(sale_types) != 1:
                    raise OzonApiError("Ozon: не найдена комиссия за продажу в справочнике")
                numbers = list(orders)
                for offset in range(work["offset"], len(numbers), 200):
                    self.progress = f"Начисления по заказам: {offset} из {len(numbers)}"
                    batch = numbers[offset:offset + 200]
                    payload = await self.finance_post("/v1/finance/accrual/postings", {"posting_numbers": batch})
                    parsed, count = parse_accruals(payload, {n: orders[n] for n in batch}, sale_types[0])
                    work["accruals"].extend(asdict(a) for a in parsed)
                    work["excluded"] += count
                    work["offset"] = offset + len(batch)
                    self.save_pending(key, work)
                names = {str(t["id"]): t.get("description") or t["name"] for t in self.types.get("accrual_types", [])}
                work.setdefault("expense_day", str(date_from))
                work.setdefault("expense_cursor", "")
                work.setdefault("expense_totals", {})
                work.setdefault("expense_seen", [])
                while date.fromisoformat(work["expense_day"]) <= date_to:
                    day = work["expense_day"]
                    done = (date.fromisoformat(day) - date_from).days
                    self.progress = f"Общие расходы: день {done + 1} из {(date_to - date_from).days + 1}"
                    cursor = work["expense_cursor"]
                    # Reuse complete daily pages across overlapping report periods.
                    # Never store a partially downloaded day as a complete snapshot.
                    now = datetime.now(ZoneInfo(self.settings.timezone))
                    age_days = (now.date() - date.fromisoformat(day)).days
                    ttl = 300 if age_days <= 0 else 3600 if age_days <= 7 else 86400
                    if not cursor:
                        with self.store.connect() as db:
                            saved_day = db.execute("SELECT fetched_at, payload FROM expense_days WHERE day=?", (day,)).fetchone()
                        if saved_day and 0 <= (now - datetime.fromisoformat(saved_day[0])).total_seconds() < ttl:
                            self.progress = f"Общие расходы: день {done + 1} из {(date_to - date_from).days + 1} — сохранённые начисления"
                            for label, cents in json.loads(saved_day[1]).items():
                                work["expense_totals"][label] = work["expense_totals"].get(label, 0) + cents
                            work["expense_day"] = str(date.fromisoformat(day) + timedelta(days=1))
                            work.pop("expense_day_totals", None)
                            work.pop("expense_day_cacheable", None)
                            self.save_pending(key, work)
                            continue
                        work["expense_day_totals"] = {}
                        work["expense_day_cacheable"] = True
                    work.setdefault("expense_day_totals", {})
                    work.setdefault("expense_day_cacheable", False)
                    payload = await self.finance_post("/v1/finance/accrual/by-day", {"date": day, "last_id": cursor})
                    entries = payload.get("accruals")
                    next_cursor = payload.get("last_id")
                    if not isinstance(entries, list) or not isinstance(next_cursor, str):
                        raise OzonApiError("Ozon: неполный ответ начислений за день")
                    seen = set(work["expense_seen"])
                    unique = []
                    for entry in entries:
                        accrual_id = str(entry.get("accrual_id") or "")
                        if not accrual_id:
                            raise OzonApiError("Ozon: начисление без идентификатора")
                        if accrual_id not in seen:
                            unique.append(entry)
                            seen.add(accrual_id)
                    if next_cursor and (next_cursor == cursor or not unique):
                        raise OzonApiError("Ozon: повторная страница начислений")
                    for label, cents in unbound_expenses(unique, names).items():
                        work["expense_totals"][label] = work["expense_totals"].get(label, 0) + cents
                        work["expense_day_totals"][label] = work["expense_day_totals"].get(label, 0) + cents
                    work["expense_seen"] = sorted(seen)
                    work["expense_cursor"] = next_cursor
                    if not next_cursor:
                        if work["expense_day_cacheable"]:
                            with self.store.connect() as db:
                                db.execute("INSERT INTO expense_days VALUES (?,?,?) ON CONFLICT(day) DO UPDATE SET fetched_at=excluded.fetched_at, payload=excluded.payload",
                                           (day, datetime.now(ZoneInfo(self.settings.timezone)).isoformat(), json.dumps(work["expense_day_totals"], ensure_ascii=False)))
                        work.pop("expense_day_totals", None)
                        work.pop("expense_day_cacheable", None)
                        work["expense_day"] = str(date.fromisoformat(day) + timedelta(days=1))
                    self.save_pending(key, work)
                accruals = [OrderAccrual(**a) for a in work["accruals"]]
                excluded = work["excluded"]
                data = dict(date_from=str(date_from), date_to=str(date_to), timezone=self.settings.timezone,
                            fetched_at=datetime.now(ZoneInfo(self.settings.timezone)).isoformat(),
                            excluded_accruals=excluded, expenses_complete=True,
                            unbound_expenses_cents=sum(work["expense_totals"].values()),
                            unbound_expenses=[dict(name=name, amount_cents=value) for name, value in sorted(work["expense_totals"].items()) if value],
                            products=[dict(product_id=p.product_id, offer_id=p.offer_id, name=p.name, skus=sorted(p.skus)) for p in products],
                            ordered=ordered, accruals=[asdict(a) for a in accruals])
                rendered = self.render(data)
                with self.store.connect() as db:
                    db.execute("INSERT INTO reports VALUES (?,?) ON CONFLICT(period) DO UPDATE SET payload=excluded.payload", (key, json.dumps(data, ensure_ascii=False)))
                    db.execute("DELETE FROM pending WHERE period=?", (key,))
                self.success_at[key] = monotonic()
                return rendered
            except OzonApiError as exc:
                self.retry_at = monotonic() + max(60, getattr(exc, "retry_seconds", 60))
                if cached:
                    return self.render(cached, True, str(exc))
                raise
