"""Общее состояние: stop_event, bot и chat_id для отправки сообщений."""
import asyncio
from typing import Dict, Optional

from aiogram import Bot

stop_event: asyncio.Event = asyncio.Event()
bot_instance: Optional[Bot] = None
"""Chat ID из .env (TG_NOTIFY_CHAT_ID); если задан, уведомления идут туда."""
notify_chat_id: Optional[int] = None
"""Chat ID юзера, который написал боту (если TG_NOTIFY_CHAT_ID не задан)."""
allowed_chat_id: Optional[int] = None
# Ожидающие выбора пользователя: key=callback_id, value=ai_result (dict)
pending_ai_results: Dict[str, dict] = {}
# Очередь выборов: (callback_id, "post"|"skip")
user_choice_queue: Optional[asyncio.Queue] = None
