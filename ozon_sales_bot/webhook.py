from __future__ import annotations

import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from .bot import SalesBotApp
from .config import Settings


logger = logging.getLogger(__name__)
WEBHOOK_PATH = "/telegram/webhook"


def resolve_base_url(settings: Settings) -> str:
    if settings.webhook_base_url:
        return settings.webhook_base_url
    railway_domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
    if railway_domain:
        return f"https://{railway_domain}"
    raise RuntimeError(
        "Для webhook-режима задайте WEBHOOK_BASE_URL или RAILWAY_PUBLIC_DOMAIN"
    )


def create_web_app(settings: Settings) -> web.Application:
    if not settings.webhook_secret:
        raise RuntimeError("Для webhook-режима задайте TELEGRAM_WEBHOOK_SECRET")

    sales_app = SalesBotApp(settings)
    sales_app.storage.initialize()
    bot = Bot(settings.telegram_bot_token)
    dispatcher = Dispatcher()
    dispatcher.include_router(sales_app.router)
    webhook_url = f"{resolve_base_url(settings)}{WEBHOOK_PATH}"

    async def health(_: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def on_startup(_: web.Application) -> None:
        await bot.set_webhook(
            url=webhook_url,
            secret_token=settings.webhook_secret,
            allowed_updates=dispatcher.resolve_used_update_types(),
            drop_pending_updates=False,
        )
        logger.info("Telegram webhook configured: %s", webhook_url)

    async def on_cleanup(_: web.Application) -> None:
        await sales_app.ozon.close()
        await bot.session.close()

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    SimpleRequestHandler(
        dispatcher=dispatcher,
        bot=bot,
        handle_in_background=False,
        secret_token=settings.webhook_secret,
    ).register(app, path=WEBHOOK_PATH)
    setup_application(app, dispatcher, bot=bot)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    port = int(os.getenv("PORT", "8080"))
    web.run_app(create_web_app(Settings.from_env()), host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()

