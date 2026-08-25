from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

from .config import Settings
from .formatting import format_report
from .keyboards import MENU_BUTTON_TEXT, main_menu, period_menu, products_menu
from .ozon import OzonApiError, OzonClient
from .request import SalesRequest, parse_sales_request
from .storage import PreferencesStorage


logger = logging.getLogger(__name__)


class SalesBotApp:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.storage = PreferencesStorage(settings.database_path)
        self.ozon = OzonClient(
            settings.ozon_api_base_url, settings.ozon_client_id, settings.ozon_api_key
        )
        self._catalog_cache: tuple[float, list] | None = None
        self._catalog_lock = asyncio.Lock()
        self.router = Router()
        self._register_handlers()

    async def _authorized(self, bot: Bot, user_id: int) -> bool:
        try:
            member = await bot.get_chat_member(
                chat_id=self.settings.allowed_telegram_group_id,
                user_id=user_id,
            )
        except TelegramAPIError:
            logger.exception("Could not check Telegram group membership")
            return False
        return member.status not in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}

    async def _deny_message(self, message: Message) -> bool:
        if message.chat.id != self.settings.allowed_telegram_group_id:
            await message.answer("Бот работает только в разрешённой группе.")
            return True
        if message.from_user and await self._authorized(message.bot, message.from_user.id):
            return False
        await message.answer("Доступ к боту запрещён.")
        return True

    async def _deny_callback(self, callback: CallbackQuery) -> bool:
        if (
            callback.message is None
            or callback.message.chat.id != self.settings.allowed_telegram_group_id
        ):
            await callback.answer("Бот работает только в разрешённой группе.", show_alert=True)
            return True
        if await self._authorized(callback.bot, callback.from_user.id):
            return False
        await callback.answer("Доступ запрещён", show_alert=True)
        return True

    def _dates(self, days: int):
        today = datetime.now(self.settings.report_timezone).date()
        return today - timedelta(days=days - 1), today

    async def show_menu_message(self, message: Message) -> None:
        if await self._deny_message(message):
            return
        preferences = self.storage.get(message.from_user.id)
        await message.answer(
            "Выберите действие. По умолчанию отчёт показывает заказы всех товаров "
            "за последние 30 дней без отменённых отправлений.",
            reply_markup=main_menu(preferences),
        )

    async def show_menu_callback(self, callback: CallbackQuery) -> None:
        if await self._deny_callback(callback):
            return
        preferences = self.storage.get(callback.from_user.id)
        await callback.message.edit_text("Настройки отчёта:", reply_markup=main_menu(preferences))
        await callback.answer()

    async def show_periods(self, callback: CallbackQuery) -> None:
        if await self._deny_callback(callback):
            return
        preferences = self.storage.get(callback.from_user.id)
        await callback.message.edit_text(
            "Выберите период заказов:", reply_markup=period_menu(preferences.period_days)
        )
        await callback.answer()

    async def set_period(self, callback: CallbackQuery) -> None:
        if await self._deny_callback(callback):
            return
        days = int(callback.data.rsplit(":", 1)[1])
        self.storage.set_period(callback.from_user.id, days)
        await callback.message.edit_text(
            f"Период изменён: {days} дней.",
            reply_markup=main_menu(self.storage.get(callback.from_user.id)),
        )
        await callback.answer("Сохранено")

    async def _load_product_catalog(self):
        now = time.monotonic()
        if self._catalog_cache is not None and self._catalog_cache[0] > now:
            return self._catalog_cache[1]
        async with self._catalog_lock:
            now = time.monotonic()
            if self._catalog_cache is not None and self._catalog_cache[0] > now:
                return self._catalog_cache[1]
            date_from, date_to = self._dates(90)
            products = await self.ozon.get_sales(date_from, date_to)
            self._catalog_cache = (time.monotonic() + 300, products)
            return products

    async def show_products(self, callback: CallbackQuery) -> None:
        if await self._deny_callback(callback):
            return
        await callback.answer("Загружаю товары…")
        page = int(callback.data.rsplit(":", 1)[1])
        try:
            products = await self._load_product_catalog()
        except OzonApiError as exc:
            logger.exception("Could not load Ozon product list")
            await callback.message.edit_text(str(exc), reply_markup=main_menu(self.storage.get(callback.from_user.id)))
            return
        preferences = self.storage.get(callback.from_user.id)
        await callback.message.edit_text(
            "Выберите номенклатуры. В списке отображаются товары из неотменённых "
            "заказов за последние 90 дней:",
            reply_markup=products_menu(products, preferences, page),
        )

    async def set_all_products(self, callback: CallbackQuery) -> None:
        if await self._deny_callback(callback):
            return
        self.storage.set_all_products(callback.from_user.id)
        await callback.answer("Выбраны все товары")
        await self.show_products_for_page(callback, 0)

    async def toggle_product(self, callback: CallbackQuery) -> None:
        if await self._deny_callback(callback):
            return
        _, _, sku, page_text = callback.data.split(":", 3)
        self.storage.toggle_sku(callback.from_user.id, sku)
        await callback.answer("Список обновлён")
        await self.show_products_for_page(callback, int(page_text))

    async def show_products_for_page(self, callback: CallbackQuery, page: int) -> None:
        try:
            products = await self._load_product_catalog()
        except OzonApiError as exc:
            await callback.message.edit_text(str(exc), reply_markup=main_menu(self.storage.get(callback.from_user.id)))
            return
        await callback.message.edit_reply_markup(
            reply_markup=products_menu(
                products, self.storage.get(callback.from_user.id), page
            )
        )

    async def send_report_callback(self, callback: CallbackQuery) -> None:
        if await self._deny_callback(callback):
            return
        await callback.answer("Формирую отчёт…")
        await self._send_report(callback.message, callback.from_user.id)

    async def send_report_command(self, message: Message) -> None:
        if await self._deny_message(message):
            return
        await self._send_report(message, message.from_user.id)

    async def send_local_request(self, message: Message) -> None:
        if await self._deny_message(message):
            return
        try:
            request = parse_sales_request(message.text or "")
        except ValueError as exc:
            await message.answer(f"Не удалось прочитать запрос: {exc}.")
            return
        await self._send_report_for_request(message, request)

    async def _send_report(self, message: Message, user_id: int) -> None:
        preferences = self.storage.get(user_id)
        await self._send_report_for_request(
            message,
            SalesRequest(preferences.period_days, preferences.selected_skus),
        )

    async def _send_report_for_request(
        self, message: Message, request: SalesRequest
    ) -> None:
        date_from, date_to = self._dates(request.period_days)
        status = await message.answer("Получаю данные из Ozon…")
        try:
            rows = await self.ozon.get_sales(date_from, date_to, request.selected_skus)
        except OzonApiError as exc:
            logger.exception("Could not build Ozon sales report")
            await status.edit_text(str(exc))
            return
        messages = format_report(rows, date_from, date_to)
        await status.edit_text(messages[0], parse_mode=ParseMode.HTML)
        for part in messages[1:]:
            await message.answer(part, parse_mode=ParseMode.HTML)

    async def noop(self, callback: CallbackQuery) -> None:
        await callback.answer()

    def _register_handlers(self) -> None:
        self.router.message.register(self.show_menu_message, CommandStart())
        self.router.message.register(self.show_menu_message, Command("menu"))
        self.router.message.register(self.show_menu_message, F.text == MENU_BUTTON_TEXT)
        self.router.message.register(self.send_report_command, Command("sales"))
        self.router.message.register(self.send_local_request, F.text.contains("#ozon_sales"))
        self.router.callback_query.register(self.show_menu_callback, F.data == "menu")
        self.router.callback_query.register(self.show_periods, F.data == "period:menu")
        self.router.callback_query.register(self.set_period, F.data.startswith("period:set:"))
        self.router.callback_query.register(self.show_products, F.data.startswith("products:"), F.data != "products:all")
        self.router.callback_query.register(self.set_all_products, F.data == "products:all")
        self.router.callback_query.register(self.toggle_product, F.data.startswith("product:toggle:"))
        self.router.callback_query.register(self.send_report_callback, F.data == "report")
        self.router.callback_query.register(self.noop, F.data == "noop")

    async def run(self) -> None:
        self.storage.initialize()
        bot = Bot(self.settings.telegram_bot_token)
        dispatcher = Dispatcher()
        dispatcher.include_router(self.router)
        try:
            await dispatcher.start_polling(bot)
        finally:
            await self.ozon.close()
            await bot.session.close()


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app = SalesBotApp(Settings.from_env())
    await app.run()


if __name__ == "__main__":
    asyncio.run(main())
