"""Отправка сообщения разрешённому юзеру (TG_USERNAME)."""
import os

from loguru import logger
import uuid

import aiohttp
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from tg_bot import context as tg_context

# Действия для инлайн-клав (все кроме help)
ACTION_BUTTONS = [
    ("place_new_order", "📌 Новый ордер"),
    ("enter_now", "▶️ Вход сейчас"),
    ("stop_breakeven", "⚖️ Breakeven"),
    ("cancel", "❌ Отмена"),
    ("close_entire", "🔻 Закрыть позицию"),
    ("info", "ℹ️ Info"),
]


def _resolve_chat_id():
    """Chat ID для уведомлений: из .env TG_NOTIFY_CHAT_ID или от юзера, писавшего боту."""
    return tg_context.notify_chat_id or tg_context.allowed_chat_id


async def send_message(text: str) -> bool:
    """
    Отправить сообщение в чат: TG_NOTIFY_CHAT_ID из .env или тому, кто уже писал боту.
    Возвращает True если отправлено, False если бот/chat_id ещё не известны.
    """
    chat_id = _resolve_chat_id()
    if tg_context.bot_instance is None or chat_id is None:
        logger.warning("tg_bot: send_message — бот или chat_id не заданы (задай TG_NOTIFY_CHAT_ID в .env или напиши боту)")
        return False
    try:
        await tg_context.bot_instance.send_message(chat_id, text)
        return True
    except Exception as e:
        logger.error("tg_bot: send_message failed: %s", e)
        return False


def _actions_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, len(ACTION_BUTTONS), 2):
        row = [
            InlineKeyboardButton(text=label, callback_data=f"act:{action}")
            for action, label in ACTION_BUTTONS[i : i + 2]
        ]
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def send_message_with_actions(text: str) -> bool:
    """
    Отправить сообщение разрешённому юзеру с инлайн-клавой: все действия кроме help.
    Возвращает True если отправлено, False если бот/chat_id не заданы.
    """
    chat_id = _resolve_chat_id()
    if tg_context.bot_instance is None or chat_id is None:
        logger.warning("tg_bot: send_message_with_actions — бот или chat_id не заданы")
        return False
    try:
        await tg_context.bot_instance.send_message(
            chat_id,
            text,
            reply_markup=_actions_keyboard(),
        )
        return True
    except Exception as e:
        logger.error("tg_bot: send_message_with_actions failed: %s", e)
        return False


def _format_ai_response(ai_result: dict) -> str:
    """Форматирует JSON от AI в блок «Ответ AI» для сообщения в ТГ."""
    parts = [
        f"<b>Action:</b> {ai_result.get('action', '?')}",
        f"<b>Trader:</b> {ai_result.get('trader', '?')}",
        f"<b>Coin:</b> {ai_result.get('coin', '?')}",
        f"<b>Side:</b> {ai_result.get('side', '?')}",
        f"<b>Entry:</b> {ai_result.get('entry')}",
        f"<b>SL:</b> {ai_result.get('sl')}",
        f"<b>TP:</b> {ai_result.get('tp')}",
        f"<b>Risk:</b> {ai_result.get('risk')}",
    ]
    return "\n".join(parts)


def _format_full_context_message(ai_result: dict) -> str:
    """Собирает полное сообщение: контекст из канала (сообщение + реплаи) + ответ AI."""
    source = ai_result.get("source_text") or ""
    ai_block = _format_ai_response(ai_result)
    if not source:
        return ai_block
    # Экранируем < и > в исходном тексте, чтобы не сломать HTML
    source_safe = source.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"<b>📩 Из канала:</b>\n<pre>{source_safe}</pre>\n\n<b>🤖 Ответ AI:</b>\n{ai_block}"


def _colored_button(text: str, callback_data: str, style: str) -> InlineKeyboardButton:
    """
    Создаёт кнопку с цветом, если текущая версия aiogram/бот API поддерживает поле style.
    В противном случае — обычную кнопку без стиля.
    """
    kwargs = {"text": text, "callback_data": callback_data}
    ann = getattr(InlineKeyboardButton, "__annotations__", {}) or {}
    if "style" in ann:
        kwargs["style"] = style
    return InlineKeyboardButton(**kwargs)


def _ai_confirm_keyboard(callback_id: str) -> InlineKeyboardMarkup:
    """Кнопки: выставить, пропустить. callback_data до 64 байт."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            _colored_button("✅", f"aic:{callback_id}:post", "success"),
            _colored_button("❌", f"aic:{callback_id}:skip", "danger"),
        ]
    ])


async def send_ai_signal(ai_result: dict) -> None:
    """
    Отправляет в ТГ текст по ответу AI и кнопки «Выставить» / «Пропустить».
    Не ждёт нажатия: сохраняет ai_result в pending_ai_results, выбор придёт через user_choice_queue.
    """
    chat_id = _resolve_chat_id()
    token = os.environ.get("TG_BOT_TOKEN", "").strip()
    if chat_id is None or not token:
        logger.warning("tg_bot: send_ai_signal — нет chat_id или TG_BOT_TOKEN")
        return

    callback_id = uuid.uuid4().hex[:12]
    tg_context.pending_ai_results[callback_id] = ai_result
    text = _format_full_context_message(ai_result)

    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": {
            "inline_keyboard": [
                [
                    {
                        "text": "✅",
                        "callback_data": f"aic:{callback_id}:post",
                        "style": "success",
                    },
                    {
                        "text": "❌",
                        "callback_data": f"aic:{callback_id}:skip",
                        "style": "danger",
                    },
                    {
                        "text": "🔄",
                        "callback_data": f"aic:{callback_id}:chg",
                        "style": "primary",
                    },
                ]
            ]
        },
    }

    api_base = os.environ.get("TG_API_BASE", "https://api.telegram.org")
    url = f"{api_base}/bot{token}/sendMessage"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.error("tg_bot: send_ai_signal raw sendMessage failed: %s %s", resp.status, body)
    except Exception as e:
        logger.error("tg_bot: send_ai_signal raw sendMessage exception: %s", e)



