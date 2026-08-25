from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.methods import GetUpdates

from .bot import SalesBotApp
from .config import Settings
from .keyboards import persistent_menu_keyboard, selector_link_keyboard


logger = logging.getLogger(__name__)


async def run_once() -> int:
    app = SalesBotApp(Settings.from_env())
    app.storage.initialize()
    bot = Bot(app.settings.telegram_bot_token)
    dispatcher = Dispatcher()
    dispatcher.include_router(app.router)
    processed = 0

    try:
        # Telegram does not allow getUpdates while a webhook is configured.
        await bot.delete_webhook(drop_pending_updates=False)
        if app.storage.get_state("menu_keyboard_version") != "1":
            await bot.send_message(
                app.settings.allowed_telegram_group_id,
                "Кнопка меню добавлена. Используйте её для настройки и получения отчёта.",
                reply_markup=persistent_menu_keyboard(),
            )
            app.storage.set_state("menu_keyboard_version", "1")
        if app.storage.get_state("selector_link_version") != "1":
            await bot.send_message(
                app.settings.allowed_telegram_group_id,
                "Теперь период и номенклатуры можно выбрать без ожидания ответов бота. "
                "После выбора отправьте в группу один готовый запрос.",
                reply_markup=selector_link_keyboard(),
            )
            app.storage.set_state("selector_link_version", "1")
        offset = app.storage.get_update_offset()

        while True:
            updates = await bot(
                GetUpdates(
                    offset=offset,
                    limit=100,
                    timeout=0,
                    allowed_updates=dispatcher.resolve_used_update_types(),
                )
            )
            if not updates:
                break

            for update in updates:
                try:
                    await dispatcher.feed_update(bot, update)
                except Exception:
                    logger.exception("Could not process Telegram update %s", update.update_id)
                finally:
                    offset = update.update_id + 1
                    app.storage.set_update_offset(offset)
                    processed += 1

            if len(updates) < 100:
                break
    finally:
        await app.ozon.close()
        await bot.session.close()

    logger.info("Processed %s Telegram updates", processed)
    return processed


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    await run_once()


if __name__ == "__main__":
    asyncio.run(main())
