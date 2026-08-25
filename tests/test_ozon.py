from datetime import date

import httpx
import pytest

from ozon_sales_bot.ozon import OzonClient


@pytest.mark.asyncio
async def test_get_sales_parses_and_filters_response():
    async def handler(request: httpx.Request) -> httpx.Response:
        body = __import__("json").loads(request.content)
        if request.url.path == "/v2/posting/fbo/list":
            assert body["filter"]["status"] == ""
            return httpx.Response(
                200,
                json={
                    "result": [
                        {
                            "status": "delivered",
                            "products": [
                                {"sku": 1, "name": "Первый", "quantity": 3},
                                {"sku": 2, "name": "Второй", "quantity": 1},
                            ],
                        },
                        {
                            "status": "cancelled",
                            "products": [{"sku": 2, "name": "Второй", "quantity": 20}],
                        },
                    ]
                },
            )
        assert request.url.path == "/v4/posting/fbs/list"
        assert body["filter"]["status"] == []
        assert body["limit"] == 100
        return httpx.Response(
            200,
            json={
                "result": {
                    "postings": [
                        {
                            "status_alias": "delivering",
                            "products": [
                                {"product_id": 2, "product_name": "Второй", "quantity": 2}
                            ],
                        }
                    ],
                    "has_next": False,
                    "cursor": "",
                }
            },
        )

    client = OzonClient("https://example.test", "client", "key")
    await client._http.aclose()
    client._http = httpx.AsyncClient(
        base_url="https://example.test", transport=httpx.MockTransport(handler)
    )
    rows = await client.get_sales(
        date(2026, 8, 1), date(2026, 8, 25), frozenset({"2"})
    )
    await client.close()

    assert len(rows) == 1
    assert rows[0].sku == "2"
    assert rows[0].ordered_units == 3
