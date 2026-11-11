from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from ozon_forecast.api import create_app


def test_web_shell_is_same_origin_and_does_not_fetch_ozon():
    source = SimpleNamespace(client=AsyncMock())
    with TestClient(create_app(source)) as client:
        assert client.get("/", follow_redirects=False).headers["location"] == "/web/"
        response = client.get("/web/")
        assert response.status_code == 200
        assert 'lang="ru"' in response.text
        assert "© ESI.Company" in response.text
        assert "viewport-fit=cover" in response.text
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        manifest = client.get("/web/manifest.webmanifest").json()
        assert manifest["scope"] == manifest["start_url"] == "/web/"
        assert manifest["display"] == "standalone"
        for icon in manifest["icons"]:
            result = client.get("/web/" + icon["src"])
            assert result.status_code == 200
            assert result.headers["content-type"] == "image/png"
        for file in ("sw.js", "app.mjs", "model.mjs", "storage.mjs", "app.css", "icons/apple-touch-icon.png"):
            result = client.get("/web/" + file)
            assert result.status_code == 200
            assert result.headers["x-content-type-options"] == "nosniff"
        for path in ("/web/.env", "/web/data/forecast.json", "/web/ozon_forecast/api.py"):
            assert client.get(path).status_code == 404
        assert client.get("/health").json()["status"] == "ok"
        assert source.client.mock_calls == []


def test_api_starts_without_web_assets(monkeypatch):
    # A Python-only Android update must still boot before web assets arrive.
    original = Path.is_dir
    monkeypatch.setattr(Path, "is_dir", lambda path: False if path.name == "web" else original(path))
    with TestClient(create_app(SimpleNamespace(client=AsyncMock()))) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/web/").status_code == 404
