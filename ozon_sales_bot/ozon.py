from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

import httpx

from .models import SaleRow


class OzonApiError(RuntimeError):
    pass


class OzonClient:
    FBO_ENDPOINT = "/v2/posting/fbo/list"
    FBS_ENDPOINT = "/v4/posting/fbs/list"
    FBO_PAGE_SIZE = 1000
    FBS_PAGE_SIZE = 100

    def __init__(self, base_url: str, client_id: str, api_key: str) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={
                "Client-Id": client_id,
                "Api-Key": api_key,
                "Content-Type": "application/json",
            },
            timeout=30,
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def get_sales(
        self,
        date_from: date,
        date_to: date,
        selected_skus: frozenset[str] | None = None,
    ) -> list[SaleRow]:
        fbo_postings, fbs_postings = await asyncio.gather(
            self._get_fbo_postings(date_from, date_to),
            self._get_fbs_postings(date_from, date_to),
        )
        return self._aggregate_postings(fbo_postings + fbs_postings, selected_skus)

    async def _post(self, endpoint: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._http.post(endpoint, json=body)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            detail = ""
            if isinstance(exc, httpx.HTTPStatusError):
                detail = f" ({exc.response.status_code}: {exc.response.text[:300]})"
            raise OzonApiError(f"Ошибка запроса к Ozon Seller API{detail}") from exc

    async def _get_fbo_postings(self, date_from: date, date_to: date) -> list[dict]:
        postings: list[dict] = []
        offset = 0
        while True:
            payload = await self._post(
                self.FBO_ENDPOINT,
                {
                    "dir": "ASC",
                    "filter": {
                        "since": f"{date_from.isoformat()}T00:00:00.000Z",
                        "to": f"{date_to.isoformat()}T23:59:59.999Z",
                        "status": "",
                    },
                    "limit": self.FBO_PAGE_SIZE,
                    "offset": offset,
                    "translit": False,
                    "with": {
                        "analytics_data": False,
                        "financial_data": False,
                        "legal_info": False,
                    },
                },
            )
            page = payload.get("result")
            if not isinstance(page, list):
                raise OzonApiError("Ozon FBO API вернул ответ неизвестного формата")
            postings.extend(page)
            if len(page) < self.FBO_PAGE_SIZE:
                return postings
            offset += len(page)

    async def _get_fbs_postings(self, date_from: date, date_to: date) -> list[dict]:
        postings: list[dict] = []
        cursor = ""
        while True:
            payload = await self._post(
                self.FBS_ENDPOINT,
                {
                    "sort_dir": "asc",
                    "filter": {
                        "since": f"{date_from.isoformat()}T00:00:00.000Z",
                        "to": f"{date_to.isoformat()}T23:59:59.999Z",
                        "status": [],
                    },
                    "limit": self.FBS_PAGE_SIZE,
                    "cursor": cursor,
                    "translit": False,
                    "with": {
                        "analytics_data": False,
                        "barcodes": False,
                        "financial_data": False,
                        "legal_info": False,
                        "translit": False,
                    },
                },
            )
            result = payload.get("result", payload)
            if not isinstance(result, dict) or not isinstance(result.get("postings"), list):
                raise OzonApiError("Ozon FBS API вернул ответ неизвестного формата")
            page = result["postings"]
            postings.extend(page)
            if not result.get("has_next"):
                return postings
            next_cursor = str(result.get("cursor") or "")
            if not next_cursor or next_cursor == cursor:
                raise OzonApiError("Ozon FBS API не вернул курсор следующей страницы")
            cursor = next_cursor

    @staticmethod
    def _aggregate_postings(
        postings: list[dict], selected_skus: frozenset[str] | None
    ) -> list[SaleRow]:
        totals: dict[str, tuple[str, int]] = {}
        for posting in postings:
            status = str(posting.get("status") or posting.get("status_alias") or "").lower()
            if "cancel" in status:
                continue
            for product in posting.get("products") or []:
                sku = str(product.get("sku") or product.get("product_id") or "").strip()
                if not sku or (selected_skus is not None and sku not in selected_skus):
                    continue
                name = str(
                    product.get("name") or product.get("product_name") or sku
                ).strip()
                try:
                    quantity = int(product.get("quantity", 0))
                except (TypeError, ValueError):
                    continue
                previous_name, previous_quantity = totals.get(sku, (name, 0))
                totals[sku] = (previous_name or name, previous_quantity + quantity)
        return sorted(
            (
                SaleRow(sku=sku, name=name, ordered_units=quantity)
                for sku, (name, quantity) in totals.items()
            ),
            key=lambda row: (-row.ordered_units, row.name.casefold()),
        )
