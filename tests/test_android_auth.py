from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import httpx
import pytest
from aiogram import Bot
from aiogram.types import Chat, Message, MessageEntity, User
from fastapi import HTTPException
from fastapi.testclient import TestClient

from ozon_forecast.api import create_app
from ozon_forecast.auth import AuthService, TelegramAccess
from ozon_forecast.models import ForecastReport
from ozon_forecast.settings import ServerSettings
from ozon_sales_bot.android_auth import AuthStore, is_group_member
from ozon_sales_bot.bot import SalesBotApp
from ozon_sales_bot.config import Settings


@pytest.mark.parametrize("status,flag,allowed", [
    ("creator", False, True), ("administrator", False, True), ("member", False, True),
    ("restricted", True, True), ("restricted", False, False), ("left", True, False),
    ("kicked", True, False), ("unknown", True, False),
])
def test_membership_statuses(status, flag, allowed):
    assert is_group_member(status, flag) is allowed


@pytest.fixture
def auth(tmp_path):
    telegram = AsyncMock()
    telegram.username.return_value = "test_sales_bot"
    telegram.member.return_value = True
    return AuthService(AuthStore(tmp_path / "auth.sqlite3", "test-scope"), telegram)


def approved_token(auth, user=123):
    challenge = auth.store.create()
    assert auth.store.approve(challenge["link_token"], user)
    return auth.store.exchange(challenge["challenge_id"], challenge["poll_secret"], user)


def test_store_binding_one_use_and_no_plaintext_tokens(auth):
    item = auth.store.create()
    assert auth.store.preview(item["link_token"]) == item["code"]
    assert auth.store.poll_user(item["challenge_id"], item["poll_secret"]) is None
    with pytest.raises(ValueError):
        auth.store.poll_user(item["challenge_id"], item["link_token"])
    assert auth.store.approve(item["link_token"], 123)
    assert not auth.store.approve(item["link_token"], 999)
    with pytest.raises(ValueError):
        auth.store.exchange(item["challenge_id"], item["poll_secret"], 999)

    def exchange():
        try:
            return auth.store.exchange(item["challenge_id"], item["poll_secret"], 123)
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: exchange(), range(2)))
    tokens = [token for token in results if token]
    assert len(tokens) == 1 and auth.store.user(tokens[0]) == 123
    assert AuthStore(auth.store.path, "other-group").user(tokens[0]) is None
    raw = auth.store.path.read_bytes()
    assert all(value.encode() not in raw for value in
               (item["link_token"], item["poll_secret"], tokens[0]))


def test_expired_challenge_and_session(auth, monkeypatch):
    now = 1_000_000.0
    monkeypatch.setattr("ozon_sales_bot.android_auth.time.time", lambda: now)
    item = auth.store.create()
    token = approved_token(auth)
    now += 301
    assert auth.store.preview(item["link_token"]) is None
    assert not auth.store.approve(item["link_token"], 123)
    with pytest.raises(ValueError):
        auth.store.poll_user(item["challenge_id"], item["poll_secret"])
    now += auth.store.SESSION_SECONDS
    assert auth.store.user(token) is None


def test_forecast_is_public_and_login_routes_are_removed(auth):
    service = AsyncMock()
    service.report.return_value = ForecastReport.model_validate({
        "period_days": 30, "sales_date_from": "2026-07-29", "sales_date_to": "2026-08-27",
        "timezone": "Europe/Moscow", "fetched_at": "2026-08-27T12:00:00+03:00", "rows": [],
    })
    with TestClient(create_app(service)) as client:
        response = client.get("/api/v1/forecast")
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        auth.telegram.member.assert_not_awaited()
        assert client.post("/auth/start").status_code == 404
        assert client.post("/auth/poll").status_code == 404
        assert client.get("/auth/session").status_code == 404
        assert client.post("/auth/logout").status_code == 404


@pytest.mark.asyncio
async def test_nonmember_cannot_exchange_and_login_rate_bound(auth):
    existing = approved_token(auth)
    item = auth.store.create()
    auth.store.approve(item["link_token"], 123)
    auth.telegram.member.return_value = False
    with pytest.raises(HTTPException) as exc:
        await auth.poll(item["challenge_id"], item["poll_secret"])
    assert exc.value.status_code == 403
    assert auth.store.user(existing) == 123
    auth.telegram.member.side_effect = HTTPException(503, "Telegram unavailable")
    with pytest.raises(HTTPException) as exc:
        await auth.poll(item["challenge_id"], item["poll_secret"])
    assert exc.value.status_code == 503
    for _ in range(20):
        await auth.start()
    with pytest.raises(HTTPException) as exc:
        await auth.start()
    assert exc.value.status_code == 429


@pytest.mark.asyncio
async def test_telegram_error_never_exposes_bot_token():
    telegram = TelegramAccess("sensitive-token", -100)
    await telegram.close()
    telegram._http = httpx.AsyncClient(
        base_url="https://api.telegram.org/botsensitive-token/",
        transport=httpx.MockTransport(lambda request: httpx.Response(429, json={"ok": False})),
    )
    with pytest.raises(HTTPException) as exc:
        await telegram.member(123)
    assert exc.value.status_code == 503
    assert "sensitive-token" not in str(exc.value)
    await telegram.close()


@pytest.mark.asyncio
async def test_bot_confirms_only_private_member_login(tmp_path):
    settings = Settings("test", "test", "test", tmp_path / "bot.sqlite3",
                        "https://example.test", ZoneInfo("Europe/Moscow"), -100,
                        forecast_auth_db=tmp_path / "auth.sqlite3")
    app = SalesBotApp(settings)
    app._authorized = AsyncMock(return_value=True)
    item = app.android_auth.create()
    # Ensure the special /start route wins before the existing group-only menu.
    bot = Bot("123456:test-token")
    incoming = Message(message_id=1, date=datetime.now(UTC), chat=Chat(id=123, type="private"),
                       from_user=User(id=123, is_bot=False, first_name="Test"),
                       text="/start apk_" + item["link_token"],
                       entities=[MessageEntity(type="bot_command", offset=0, length=6)])
    matched, _ = await app.router.message.handlers[0].check(incoming, bot=bot)
    assert matched
    await bot.session.close()
    message = SimpleNamespace(chat=SimpleNamespace(type="private", id=123),
                              from_user=SimpleNamespace(id=123, is_bot=False),
                              bot=object(), answer=AsyncMock(), edit_text=AsyncMock())
    callback = SimpleNamespace(message=message, from_user=message.from_user,
                               bot=object(), data="apk:" + item["link_token"], answer=AsyncMock())
    await app.android_login(message, SimpleNamespace(args="apk_" + item["link_token"]))
    assert item["code"] in message.answer.call_args.args[0]
    assert app.android_auth.poll_user(item["challenge_id"], item["poll_secret"]) is None
    message.chat.type = "group"
    await app.android_confirm(callback)
    assert app.android_auth.poll_user(item["challenge_id"], item["poll_secret"]) is None
    message.chat.type = "private"
    app._authorized.return_value = False
    await app.android_confirm(callback)
    assert app.android_auth.poll_user(item["challenge_id"], item["poll_secret"]) is None
    app._authorized.return_value = True
    await app.android_confirm(callback)
    assert app.android_auth.poll_user(item["challenge_id"], item["poll_secret"]) == 123
    await app.ozon.close()


def test_server_does_not_require_telegram_config(monkeypatch, tmp_path):
    monkeypatch.setenv("FORECAST_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.setenv("OZON_CLIENT_ID", "1")
    monkeypatch.setenv("OZON_API_KEY", "test")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ALLOWED_TELEGRAM_GROUP_ID", raising=False)
    assert ServerSettings.from_env().client_id == "1"
