import asyncio

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from loguru import logger

router = Router(name="commands")

@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer("Бот запущен. Доступные команды: /start, /stop")


@router.message(Command("stop"))
async def cmd_stop(message: Message, stop_event: asyncio.Event = None, shutdown_callback=None) -> None:
    logger.info(f"tg_bot: /stop от user_id={message.from_user.id}")
    await message.answer("Останавливаю бота.")
    if shutdown_callback is not None:
        await shutdown_callback()
    elif stop_event is not None:
        stop_event.set()
