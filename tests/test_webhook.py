from types import SimpleNamespace

from ozon_sales_bot.webhook import resolve_base_url


def test_resolve_base_url_prefers_explicit_value(monkeypatch):
    monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "ignored.example")
    settings = SimpleNamespace(webhook_base_url="https://bot.example")

    assert resolve_base_url(settings) == "https://bot.example"


def test_resolve_base_url_uses_railway_domain(monkeypatch):
    monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "bot.up.railway.app")
    settings = SimpleNamespace(webhook_base_url=None)

    assert resolve_base_url(settings) == "https://bot.up.railway.app"
