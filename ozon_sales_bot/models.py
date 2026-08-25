from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True)
class SaleRow:
    sku: str
    name: str
    ordered_units: int


@dataclass(frozen=True, slots=True)
class UserPreferences:
    period_days: int = 30
    selected_skus: frozenset[str] | None = None
    date_from: date | None = None
    date_to: date | None = None

    @property
    def all_products(self) -> bool:
        return self.selected_skus is None

    @property
    def custom_period(self) -> bool:
        return self.date_from is not None and self.date_to is not None
