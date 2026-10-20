from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from pydantic import BaseModel, Field


@dataclass(frozen=True)
class Product:
    product_id: str
    offer_id: str
    name: str
    skus: frozenset[str]


@dataclass(frozen=True)
class Inventory:
    available: int = 0
    in_transit: int = 0


class ClusterRow(BaseModel):
    name: str
    scheme: str = Field(pattern=r"^(FBO|FBS)$")
    sales: int = Field(ge=0)
    stock: int = Field(ge=0)
    in_transit: int = Field(ge=0)


class ForecastRow(BaseModel):
    product_id: str
    offer_id: str
    name: str
    skus: list[str]
    sales_30_days: int = Field(ge=0)
    stock: int = Field(ge=0)
    in_transit: int = Field(ge=0)
    forecast_units: int = Field(ge=0)
    to_supply: int = Field(ge=0)
    clusters: list[ClusterRow] = Field(default_factory=list)


class ForecastReport(BaseModel):
    schema_version: int = 1
    period_days: int
    sales_period_days: int = 30
    sales_date_from: date
    sales_date_to: date
    timezone: str
    fetched_at: datetime
    stale: bool = False
    warning: str | None = None
    rows: list[ForecastRow]


@dataclass(frozen=True)
class WarehouseInventory:
    sku: str
    warehouse_name: str
    scheme: str
    stock: int = 0
    in_transit: int = 0


class WarehouseStockRow(BaseModel):
    warehouse_name: str
    scheme: str = Field(pattern=r"^(FBO|FBS)$")
    stock: int = Field(ge=0)
    in_transit: int = Field(ge=0)


class WarehouseProductRow(BaseModel):
    product_id: str
    offer_id: str
    name: str
    skus: list[str]
    warehouses: list[WarehouseStockRow]


class WarehouseStockReport(BaseModel):
    schema_version: int = 1
    timezone: str
    fetched_at: datetime
    rows: list[WarehouseProductRow]


def units_to_supply(
    sales: int,
    days: int,
    stock: int,
    in_transit: int,
    sales_days: int = 30,
) -> int:
    """Exact integer equivalent of the forecast formula without floating point."""
    values = (sales, days, stock, in_transit, sales_days)
    if any(isinstance(x, bool) or not isinstance(x, int) for x in values):
        raise ValueError("Расчёт принимает только целые числа")
    if not 1 <= days <= 365 or not 1 <= sales_days <= 365 or min(sales, stock, in_transit) < 0:
        raise ValueError("Периоды должны быть от 1 до 365 дней, количества — неотрицательными")
    numerator = sales * days - sales_days * (stock + in_transit)
    return max(0, (numerator + sales_days - 1) // sales_days)
