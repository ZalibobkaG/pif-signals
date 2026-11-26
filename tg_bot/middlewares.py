"""Проверка: только TG_USERNAME может писать боту. Сохраняем chat_id для отправки уведомлений."""
from loguru import logger
import os
from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject

from tg_bot import context as tg_context

ALLOWED_USERNAME = (os.environ.get("TG_USERNAME") or "").lstrip("@").lower()


class AllowedUserMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: dict):
        if not isinstance(event, Message) or not event.text:
            return await handler(event, data)
        user = event.from_user
        username = (user.username or "").lower()
        if ALLOWED_USERNAME and username != ALLOWED_USERNAME:
            logger.warning(f"tg_bot: доступ отклонён username={username!r} (ожидается {ALLOWED_USERNAME!r})")
            await event.answer("⛔ Доступ запрещён: бот работает только для владельца.")
            return
        tg_context.bot_instance = event.bot
        tg_context.allowed_chat_id = event.chat.id
        return await handler(event, data)
