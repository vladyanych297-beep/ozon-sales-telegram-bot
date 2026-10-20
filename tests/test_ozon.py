from datetime import date

import httpx
import pytest

from ozon_sales_bot.ozon import OzonClient


@pytest.mark.asyncio
async def test_get_sales_parses_and_filters_response():
    async def handler(request: httpx.Request) -> httpx.Response:
        body = __import__("json").loads(request.content)
        if request.url.path == "/v3/posting/fbo/list":
            assert body["filter"]["status"] == []
            assert body["cursor"] == ""
            assert body["limit"] == 100
            assert body["with"]["financial_data"] is False
            return httpx.Response(
                200,
                json={
                    "postings": [
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
                    ],
                    "has_next": False,
                    "cursor": "",
                },
            )
        assert request.url.path == "/v4/posting/fbs/list"
        assert body["filter"]["status"] == []
        assert body["limit"] == 100
        assert body["with"]["financial_data"] is False
        return httpx.Response(
            200,
            json={
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


@pytest.mark.asyncio
async def test_fbo_v3_uses_cursor_pagination():
    client = OzonClient("https://example.test", "client", "key")
    calls = []

    async def post(endpoint, body):
        calls.append((endpoint, body["cursor"]))
        if not body["cursor"]:
            return {"postings": [{"products": []}], "has_next": True, "cursor": "next"}
        return {"postings": [{"products": []}], "has_next": False, "cursor": ""}

    client._post = post
    result = await client._get_fbo_postings(date(2026, 9, 1), date(2026, 9, 12))
    await client.close()

    assert len(result) == 2
    assert calls == [("/v3/posting/fbo/list", ""), ("/v3/posting/fbo/list", "next")]
