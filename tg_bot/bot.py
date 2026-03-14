"""
Точка входа бота. Сейчас: python tg_bot/bot.py из корня. Потом: таск в main.py.
Токен: TG_BOT_TOKEN, только пользователь: TG_USERNAME (например @your_username)
"""
import asyncio
import os
import sys

from loguru import logger
from pathlib import Path

if __name__ == "__main__":
    _root = Path(__file__).resolve().parent.parent
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

load_dotenv()

from tg_bot import context as tg_context
from tg_bot.handlers import commands_router, ai_confirm_router
from tg_bot.middlewares import AllowedUserMiddleware

TOKEN = os.environ.get("TG_BOT_TOKEN", "")


def _apply_notify_chat_id():
    """Читает TG_NOTIFY_CHAT_ID из .env и выставляет в контекст."""
    raw = os.environ.get("TG_NOTIFY_CHAT_ID", "").strip()
    if raw:
        try:
            tg_context.notify_chat_id = int(raw)
            logger.info("tg_bot: TG_NOTIFY_CHAT_ID задан в .env, уведомления в этот чат")
        except ValueError:
            logger.warning("tg_bot: TG_NOTIFY_CHAT_ID должен быть числом, получено: %r", raw)


class StopEventMiddleware:
    def __init__(self, stop_event: asyncio.Event = None, shutdown_callback=None):
        self.stop_event = stop_event
        self.shutdown_callback = shutdown_callback

    async def __call__(self, handler, event, data: dict):
        data["stop_event"] = self.stop_event
        data["shutdown_callback"] = self.shutdown_callback
        return await handler(event, data)


async def main(shutdown_callback=None) -> None:
    if not TOKEN:
        logger.error("TG_BOT_TOKEN не задан в .env")
        return
    _apply_notify_chat_id()
    stop_event = asyncio.Event() if shutdown_callback is None else None
    bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    tg_context.bot_instance = bot
    dp = Dispatcher()
    dp.message.middleware(AllowedUserMiddleware())
    dp.message.middleware(StopEventMiddleware(stop_event=stop_event, shutdown_callback=shutdown_callback))
    dp.include_router(commands_router)
    dp.include_router(ai_confirm_router)

    if shutdown_callback is not None:
        await dp.start_polling(bot)
        return
    polling_task = asyncio.create_task(dp.start_polling(bot))
    try:
        await stop_event.wait()
    except asyncio.CancelledError:
        pass
    finally:
        polling_task.cancel()
        try:
            await polling_task
        except asyncio.CancelledError:
            pass
        await bot.session.close()
    logger.info("Бот остановлен.")


async def run_with_shutdown(shutdown_callback) -> None:
    """Точка входа при запуске из main.py: по /stop вызывается shutdown_callback (full_shutdown)."""
    logger.info("TG bot task started.")
    if not TOKEN:
        logger.error("TG_BOT_TOKEN не задан в .env — тг бот не запущен.")
        return
    logger.info("TG bot starting (polling)...")
    await main(shutdown_callback=shutdown_callback)


if __name__ == "__main__":
    asyncio.run(main())
