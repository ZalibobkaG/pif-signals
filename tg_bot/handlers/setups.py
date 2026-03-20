from aiogram import Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from loguru import logger

from tg_bot import context as tg_context

router = Router(name="setups")


def _get_trades_dict_snapshot() -> dict:
    """
    Берем снимок trades_dict, чтобы не держать гонку при чтении.
    Значения (dict внутри) не копируем глубоко — достаточно для списка coins.
    """
    d = tg_context.trades_dict
    if not isinstance(d, dict):
        return {}
    return dict(d)


def _extract_side_label(t: dict) -> str:
    """
    Из trades_dict записи берём направление `side` для отображения:
    должно быть long/short.
    """
    side = t.get("side")
    if not isinstance(side, str):
        return ""

    s = side.strip().lower()
    if s in ("long", "short"):
        return s
    # На случай, если где-то хранится как Buy/Sell
    if s in ("buy", "b") or s.startswith("long"):
        return "long"
    if s in ("sell", "s") or s.startswith("short"):
        return "short"
    return ""


def _coin_short_label(coin: str) -> str:
    """
    Убираем хвост `USDT`, чтобы показывать пользователю только тикер.
    Пример: XRPUSDT -> XRP
    """
    c = (coin or "").strip().upper()
    if c.endswith("USDT"):
        return c[: -len("USDT")]
    return c


def _collect_setups(exchange: str) -> list[tuple[int, str, str]]:
    snapshot = _get_trades_dict_snapshot()
    # (message_id, coin, side_label)
    setups: list[tuple[int, str, str]] = []

    for message_id, t in snapshot.items():
        if not isinstance(t, dict):
            continue
        if t.get("exchange") != exchange:
            continue
        coin = (t.get("coin") or "").strip()
        if not coin:
            continue
        side_label = _extract_side_label(t)
        if not side_label:
            # На крайний случай: если side нет, не показываем направление.
            side_label = ""

        # message_id обычно int (ключи словаря). На всякий случай пытаемся привести.
        try:
            mid = int(message_id)
        except Exception:
            continue

        setups.append((mid, coin, side_label))

    # Стабильный порядок: по message_id
    setups.sort(key=lambda x: x[0])
    return setups


def _action_keyboard() -> InlineKeyboardMarkup:
    """
    Сначала выбираем действие, потом биржу.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🗑 Delete", callback_data="setups:choose_exchange:delete"),
                InlineKeyboardButton(text="🔄 Update", callback_data="setups:choose_exchange:update"),
            ]
        ]
    )


def _exchange_menu_keyboard(action: str) -> InlineKeyboardMarkup:
    """
    action: delete|update
    """
    if action == "delete":
        bybit_cb = "setups:ex:delete:bybit"
        okx_cb = "setups:ex:delete:okx"
    else:
        bybit_cb = "setups:ex:update:bybit"
        okx_cb = "setups:ex:update:okx"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Bybit", callback_data=bybit_cb)],
            [InlineKeyboardButton(text="OKX", callback_data=okx_cb)],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="setups:act_menu")],
        ]
    )


def _exchange_setups_keyboard(exchange: str, setups: list[tuple[int, str, str]]) -> InlineKeyboardMarkup:
    # Чтобы не упереться в ограничения Telegram на размер reply_markup.
    max_buttons = 30
    setups_shown = setups[:max_buttons]

    inline_keyboard: list[list[InlineKeyboardButton]] = []
    # В каждой строке делаем по 2 кнопки (по 2 монеты).
    for i in range(0, len(setups_shown), 2):
        row: list[InlineKeyboardButton] = []
        for message_id, coin, side_label in setups_shown[i : i + 2]:
            coin_short = _coin_short_label(coin)
            # Формат кнопки: XRP long/short
            btn_text = coin_short if not side_label else f"{coin_short} {side_label}"
            row.append(
                InlineKeyboardButton(
                    text=btn_text,
                    callback_data=f"setups:del:{exchange}:{message_id}",
                )
            )
        if row:
            inline_keyboard.append(row)

    inline_keyboard.append(
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="setups:choose_exchange:delete")]
    )

    # Если монет больше, чем показываем кнопками — все равно выведем их в тексте.
    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)


def _exchange_screen_text(exchange: str, setups: list[tuple[int, str, str]]) -> str:
    if not setups:
        return "Сетапов нет."

    return f"Выберите сетап для удаления ({exchange})."


@router.message(Command("setups"))
async def cmd_setups(message: Message) -> None:
    await message.answer("Выберите действие:", reply_markup=_action_keyboard())


@router.callback_query(lambda c: bool(c.data) and c.data == "setups:act_menu")
async def on_setups_act_menu(callback: CallbackQuery) -> None:
    try:
        await callback.answer()
        if not callback.message:
            return
        await callback.message.edit_text("Выберите действие:", reply_markup=_action_keyboard())
    except Exception as e:
        logger.warning("tg_bot: setups act_menu failed: %s", e)


@router.callback_query(lambda c: bool(c.data) and c.data in ("setups:choose_exchange:delete", "setups:choose_exchange:update"))
async def on_setups_choose_exchange(callback: CallbackQuery) -> None:
    try:
        await callback.answer()
        if not callback.data or not callback.message:
            return
        _, _, action = callback.data.split(":", 2)
        await callback.message.edit_text(
            "Выберите биржу:",
            reply_markup=_exchange_menu_keyboard(action),
        )
    except Exception as e:
        logger.warning("tg_bot: setups choose_exchange failed: %s", e)


@router.callback_query(lambda c: bool(c.data) and c.data.startswith("setups:ex:delete:"))
async def on_setups_exchange_delete(callback: CallbackQuery) -> None:
    try:
        await callback.answer()
        if not callback.data or not callback.message:
            return
        # setups:ex:delete:{bybit|okx}
        _, _, _, ex = callback.data.split(":", 3)
        exchange = "BYBIT" if ex == "bybit" else "OKX"
        setups = _collect_setups(exchange)
        await callback.message.edit_text(
            _exchange_screen_text(exchange, setups),
            reply_markup=_exchange_setups_keyboard(exchange, setups),
        )
    except Exception as e:
        logger.warning("tg_bot: setups exchange delete failed: %s", e)


@router.callback_query(lambda c: bool(c.data) and c.data.startswith("setups:ex:update:"))
async def on_setups_exchange_update(callback: CallbackQuery) -> None:
    try:
        await callback.answer()
        if not callback.data or not callback.message:
            return
        # setups:ex:update:{bybit|okx}
        _, _, _, ex = callback.data.split(":", 3)
        exchange = "BYBIT" if ex == "bybit" else "OKX"
        await callback.message.edit_text(
            f"Update для {exchange} пока заглушка.",
            reply_markup=_action_keyboard(),
        )
    except Exception as e:
        logger.warning("tg_bot: setups exchange update failed: %s", e)


@router.callback_query(lambda c: bool(c.data) and c.data.startswith("setups:del:"))
async def on_setups_coin(callback: CallbackQuery) -> None:
    try:
        await callback.answer()
        if not callback.data or not callback.message:
            return

        user_id = getattr(callback.from_user, "id", None)

        # setups:del:{exchange}:{message_id}
        _, _, exchange, message_id_s = callback.data.split(":", 3)
        exchange = exchange.strip().upper()
        message_id_s = message_id_s.strip()

        d = tg_context.trades_dict
        if not isinstance(d, dict):
            return

        # Удаляем именно этот сетап (по ключу message_id).
        # Стратегия больше не увидит эту запись.
        deleted = False
        deleted_payload = None

        if message_id_s in d:
            deleted_payload = d.get(message_id_s)
            d.pop(message_id_s, None)
            deleted = True
        else:
            try:
                mid_int = int(message_id_s)
            except Exception:
                mid_int = None
            if mid_int is not None and mid_int in d:
                deleted_payload = d.get(mid_int)
                d.pop(mid_int, None)
                deleted = True

        if not deleted:
            logger.debug(f"tg_bot: setups del: не найден message_id={message_id_s!r} exchange={exchange}")
        else:
            if isinstance(deleted_payload, dict):
                coin = deleted_payload.get("coin")
                side_label = _extract_side_label(deleted_payload)
            else:
                coin = None
                side_label = None
            logger.info(
                f"tg_bot: /setups delete clicked user_id={user_id!r} exchange={exchange} "
                f"message_id={message_id_s!r} coin={coin!r} side={side_label!r}"
            )

        setups = _collect_setups(exchange)
        await callback.message.edit_text(
            _exchange_screen_text(exchange, setups),
            reply_markup=_exchange_setups_keyboard(exchange, setups),
        )
    except Exception as e:
        logger.warning("tg_bot: setups coin callback failed: %s", e)
        if callback.message:
            await callback.message.answer("Не удалось удалить сетап. Попробуй еще раз.")

