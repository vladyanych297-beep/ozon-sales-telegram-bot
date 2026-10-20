from __future__ import annotations

import asyncio
import re
import time
from collections import deque

import httpx
from fastapi import HTTPException

from ozon_sales_bot.android_auth import AuthStore, is_group_member


class TelegramAccess:
    def __init__(self, token: str, group_id: int) -> None:
        self.group_id = group_id
        self._http = httpx.AsyncClient(
            base_url=f"https://api.telegram.org/bot{token}/", timeout=15,
            follow_redirects=False,
        )
        self._username: str | None = None

    async def close(self) -> None:
        await self._http.aclose()

    async def _call(self, method: str, data: dict) -> dict:
        try:
            response = await self._http.post(method, json=data)
            if response.status_code != 200:
                raise ValueError()
            payload = response.json()
            if payload.get("ok") is not True or not isinstance(payload.get("result"), dict):
                raise ValueError()
            return payload["result"]
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            # Telegram request URLs contain the bot token: never forward/log exceptions.
            raise HTTPException(503, "Не удалось проверить доступ через Telegram. Повторите позже.") from None

    async def username(self) -> str:
        if self._username is None:
            me = await self._call("getMe", {})
            member = await self._call("getChatMember", {
                "chat_id": self.group_id, "user_id": me.get("id"),
            })
            username = me.get("username", "")
            if member.get("status") not in {"administrator", "creator"} or not re.fullmatch(
                r"[A-Za-z0-9_]{5,32}", username
            ):
                raise HTTPException(503, "Администратору нужно проверить настройки Telegram-бота.")
            self._username = username
        return self._username

    async def member(self, user_id: int) -> bool:
        result = await self._call("getChatMember", {"chat_id": self.group_id, "user_id": user_id})
        return is_group_member(result.get("status", ""), result.get("is_member", False))


class AuthService:
    def __init__(self, store: AuthStore, telegram: TelegramAccess) -> None:
        self.store, self.telegram = store, telegram
        # Global bound is intentional: no trust in spoofable X-Forwarded-For headers.
        self._starts: deque[float] = deque()
        self._member_slots = asyncio.Semaphore(10)

    async def start(self) -> dict:
        now = time.monotonic()
        while self._starts and self._starts[0] < now - 60:
            self._starts.popleft()
        if len(self._starts) >= 20:
            raise HTTPException(429, "Слишком много попыток входа. Повторите через минуту.")
        self._starts.append(now)
        username = await self.telegram.username()
        try:
            result = self.store.create()
        except OverflowError as exc:
            raise HTTPException(429, str(exc)) from None
        result["telegram_url"] = f"https://t.me/{username}?start=apk_{result.pop('link_token')}"
        return result

    async def check_member(self, user_id: int) -> None:
        async with self._member_slots:
            allowed = await self.telegram.member(user_id)
        if not allowed:
            raise HTTPException(403, "Доступ разрешён только участникам Telegram-группы.")

    async def poll(self, challenge: str, secret: str) -> dict:
        try:
            user = self.store.poll_user(challenge, secret)
            if user is None:
                return {"status": "pending"}
            await self.check_member(user)
            token = self.store.exchange(challenge, secret, user)
        except ValueError as exc:
            raise HTTPException(401, str(exc)) from None
        return {"status": "approved", "access_token": token,
                "expires_in": self.store.SESSION_SECONDS}

    def token(self, authorization: str | None) -> str:
        if not authorization or not re.fullmatch(r"Bearer [A-Za-z0-9_-]{43}", authorization):
            raise HTTPException(401, "Войдите через Telegram.")
        return authorization[7:]

    async def authorize(self, authorization: str | None) -> int:
        token = self.token(authorization)
        user = self.store.user(token)
        if user is None:
            raise HTTPException(401, "Сессия истекла. Войдите через Telegram заново.")
        # Membership is checked when issuing the session, not on later requests.
        return user
