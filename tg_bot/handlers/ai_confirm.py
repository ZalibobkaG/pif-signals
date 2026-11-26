"""Обработка нажатий «Выставить» / «Пропустить» по сообщению с сигналом AI."""
from __future__ import annotations
import os

from loguru import logger

import aiohttp
from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery

from tg_bot import context as tg_context

router = Router(name="ai_confirm")


def _callback_answer_expired(exc: TelegramBadRequest) -> bool:
    m = (exc.message or "").lower()
    return "query is too old" in m or "query id is invalid" in m


async def safe_callback_answer(callback: CallbackQuery, text: str | None = None, show_alert: bool = False) -> None:
    try:
        await callback.answer(text=text, show_alert=show_alert)
    except TelegramBadRequest as e:
        if _callback_answer_expired(e):
            logger.debug("ai_confirm: устарел callback_query, answer пропущен: %s", e.message)
            return
        raise


@router.callback_query(lambda c: c.data and c.data.startswith("aic:"))
async def on_ai_confirm(callback: CallbackQuery) -> None:
    """
    Форматы callback_data:
    - aic:{id}:post        — подтвердить действие как есть
    - aic:{id}:skip        — пропустить
    - aic:{id}:chg         — показать выбор других действий
    - aic:{id}:act:{name}  — выбрать другое действие и подтвердить (как post)
    """
    if not callback.data:
        await safe_callback_answer(callback)
        return

    parts = callback.data.split(":")
    if len(parts) < 3:
        await safe_callback_answer(callback)
        return
    _, callback_id, cmd, *rest = parts

    token = os.environ.get("TG_BOT_TOKEN", "").strip()
    chat_id = callback.message.chat.id if callback.message else None
    message_id = callback.message.message_id if callback.message else None
    api_base = os.environ.get("TG_API_BASE", "https://api.telegram.org")

    async def _edit_markup(reply_markup: dict) -> None:
        if not (token and chat_id is not None and message_id is not None):
            return
        url = f"{api_base}/bot{token}/editMessageReplyMarkup"
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": reply_markup,
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.warning(
                            "ai_confirm: raw editMessageReplyMarkup failed: %s %s",
                            resp.status,
                            body,
                        )
        except Exception as e:
            logger.warning("ai_confirm: exception on raw editMessageReplyMarkup: %s", e)

    async def _finalize(choice, action_label=None) -> None:
        # Сообщаем в стратегию
        if tg_context.user_choice_queue is not None:
            tg_context.user_choice_queue.put_nowait((callback_id, choice))
        # Одинокая цветная кнопка с финальным состоянием
        if choice == "post" and action_label:
            btn_text = f"✅ — {action_label}"
        else:
            btn_text = "✅" if choice == "post" else "❌"

        reply_markup = {
            "inline_keyboard": [
                [
                    {
                        "text": btn_text,
                        "callback_data": "aic:done",
                        "style": "success" if choice == "post" else "danger",
                    }
                ]
            ]
        }
        await _edit_markup(reply_markup)
        await safe_callback_answer(
            callback, "Принято" if choice == "post" else "Пропущено"
        )

    # 1) Подтвердить / пропустить
    if cmd in ("post", "skip"):
        await _finalize(cmd)
        return

    # 2) Попросили изменить действие — показываем клавиатуру со всеми вариантами
    if cmd == "chg":
        actions = [
            ("place_new_order", "📌 Новый ордер"),
            ("enter_now", "▶️ Вход сейчас"),
            ("stop_breakeven", "⚖️ Breakeven"),
            ("cancel", "❌ Отмена"),
            ("close_entire", "🔻 Закрыть позицию"),
            ("info", "ℹ️ Info"),
        ]
        rows = []
        for i in range(0, len(actions), 2):
            row = []
            for key, label in actions[i : i + 2]:
                row.append(
                    {
                        "text": label,
                        "callback_data": f"aic:{callback_id}:act:{key}",
                    }
                )
            rows.append(row)
        reply_markup = {"inline_keyboard": rows}
        await _edit_markup(reply_markup)
        await safe_callback_answer(callback, "Выбери действие")
        return

    # 3) Выбрано другое действие: aic:{id}:act:{name}
    if cmd == "act" and rest:
        new_action = rest[0]
        order = tg_context.pending_ai_results.get(callback_id)
        if isinstance(order, dict):
            order["action"] = new_action
        # В финальной кнопке показываем raw action, без маппинга
        action_label = new_action
        # Дальше действуем как по нажатию ✅
        await _finalize("post", action_label=action_label)
        return

    await safe_callback_answer(callback)
