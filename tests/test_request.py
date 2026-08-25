import base64
import json

import pytest

from ozon_sales_bot.request import parse_sales_request


def encode(payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def test_parse_all_products_request():
    request = parse_sales_request(f"#ozon_sales {encode({'p': 30, 's': 'all'})}")

    assert request.period_days == 30
    assert request.selected_skus is None


def test_parse_selected_products_with_share_link():
    encoded = encode({"p": 7, "s": ["123", "ABC-7"]})
    request = parse_sales_request(f"Страница: https://example.test\n#ozon_sales {encoded}")

    assert request.period_days == 7
    assert request.selected_skus == frozenset({"123", "ABC-7"})


@pytest.mark.parametrize(
    "text",
    [
        "#ozon_sales invalid",
        lambda: f"#ozon_sales {encode({'p': 10, 's': 'all'})}",
        lambda: f"#ozon_sales {encode({'p': 30, 's': []})}",
    ],
)
def test_reject_invalid_request(text):
    value = text() if callable(text) else text
    with pytest.raises(ValueError):
        parse_sales_request(value)
