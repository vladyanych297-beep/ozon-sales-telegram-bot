from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SaleRow:
    sku: str
    name: str
    ordered_units: int


@dataclass(frozen=True, slots=True)
class UserPreferences:
    period_days: int = 30
    selected_skus: frozenset[str] | None = None

    @property
    def all_products(self) -> bool:
        return self.selected_skus is None
