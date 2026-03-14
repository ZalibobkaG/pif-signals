from asyncio import Queue
import asyncio
import os as _os

from loguru import logger

from exchanges.okx.trade import OkxTrade
from exchanges.bybit.trade import BybitTrade
from exchanges.okx.ws_process import ws_process
from tg_bot import context as tg_context
from tg_bot.notify import send_message, send_ai_signal


async def okx_strategy_pool(ai_response_queue: Queue, user_choice_queue: Queue, trades_dict: dict, bybit_queue: Queue = None, okx_trade: OkxTrade = None, bybit_trade: BybitTrade = None, okx_ws_queue: Queue = None):
    """
    Основной цикл обработки ответов AI для OKX.
    - Из ai_response_queue только отправляем в ТГ (send_ai_signal), не ждём ответа.
    - Выбор пользователя приходит в user_choice_queue; по «Выставить» выполняем действие.
    """
    while True:
        try:
            while True:
                try:
                    new_order = ai_response_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                logger.info(f" ✅ okx_strategy_pool: new_order: {new_order}")
                if isinstance(new_order, dict):
                    await send_ai_signal(new_order)
        except Exception as e:
            logger.exception("okx_strategy_pool send_ai_signal: %s", e)

        try:
            cb_id, choice = user_choice_queue.get_nowait()
        except asyncio.QueueEmpty:
            cb_id, choice = None, None
        if cb_id is not None and choice is not None:
            new_order = tg_context.pending_ai_results.pop(cb_id, None)
            if choice != "post":
                logger.info(" ⏭️ Пользователь выбрал «Пропустить»")
            elif new_order is None:
                logger.warning(" ⏭️ Нет данных по callback_id %s", cb_id)
            else:
                action = new_order.get("action")
                message_id = new_order.get("message_id")
                if not action:
                    logger.warning(" ❌ AI response missing action: %r", new_order)
                else:
                    # Обработка действий, которые не требуют coin
                    if action == "info":
                        logger.info(" ⛏️ OKX info - skip")
                    elif action == "help":
                        logger.info(" 😣 OKX help")
                    else:
                        coin = new_order.get("coin")
                        if coin:
                            coin = coin.upper()
                            new_order["coin"] = coin
                        if not coin:
                            if message_id in trades_dict:
                                coin = trades_dict[message_id].get("coin")
                            if not coin:
                                logger.warning(" ❌ AI response missing coin and message_id not in trades_dict: %r", new_order)
                        if coin:
                            okx_coin = f"{coin[:-4]}-{coin[-4:]}-SWAP"
                            if message_id not in trades_dict:
                                # Решаем, куда везти новый ордер:
                                # 1) Если по этой монете уже есть ордер на BYBIT и нет на OKX → пробуем открыть на OKX
                                # 2) Иначе: Bybit основной, OKX — fallback
                                active_statuses = {"active", "filled", "partially_filled"}
                                has_bybit_for_coin = any(
                                    t.get("coin") == coin
                                    and t.get("exchange") == "BYBIT"
                                    and t.get("status") in active_statuses
                                    for t in trades_dict.values()
                                )
                                has_okx_for_coin = any(
                                    t.get("coin") == coin
                                    and t.get("exchange") == "OKX"
                                    and t.get("status") in active_statuses
                                    for t in trades_dict.values()
                                )

                                prefer_okx = has_bybit_for_coin and not has_okx_for_coin

                                if prefer_okx:
                                    # Уже есть сетап на Bybit по этой монете, пробуем увести новый на OKX
                                    available_info = await okx_trade.get_instrument_info(okx_coin)
                                    if available_info[0] is not None:
                                        logger.info(f"{coin} already on BYBIT, route new order to OKX")
                                        new_order["exchange"] = "OKX"
                                        trades_dict[new_order["message_id"]] = new_order.copy()
                                        trades_dict[new_order["message_id"]]["instrument_info"] = available_info
                                    else:
                                        logger.warning(f"{coin} has BYBIT trade but not listed on OKX, stay on BYBIT")
                                        available_info = await bybit_trade.get_instrument_info(coin) if bybit_trade else (None, None, None, None)
                                        if available_info[0] is not None:
                                            new_order["exchange"] = "BYBIT"
                                            trades_dict[new_order["message_id"]] = new_order.copy()
                                            trades_dict[new_order["message_id"]]["instrument_info"] = available_info
                                        else:
                                            logger.warning(f"{coin} not available on Bybit nor OKX")
                                else:
                                    # Bybit основной, OKX вспомогательный
                                    available_info = await bybit_trade.get_instrument_info(coin) if bybit_trade else (None, None, None, None)
                                    if available_info[0] is not None:
                                        logger.info(f"{coin} available on Bybit (primary)")
                                        new_order["exchange"] = "BYBIT"
                                        trades_dict[new_order["message_id"]] = new_order.copy()
                                        trades_dict[new_order["message_id"]]["instrument_info"] = available_info
                                    else:
                                        available_info = await okx_trade.get_instrument_info(okx_coin)
                                        if available_info[0] is None:
                                            logger.warning(f"{coin} not available on Bybit nor OKX")
                                        else:
                                            logger.warning(f"{coin} not on Bybit - go to OKX")
                                            new_order["exchange"] = "OKX"
                                            trades_dict[new_order["message_id"]] = new_order.copy()
                                            trades_dict[new_order["message_id"]]["instrument_info"] = available_info
                                        
                            if message_id in trades_dict and trades_dict[new_order["message_id"]]["exchange"] == "OKX":
                                # Обработка action на OKX
                                if action == "place_new_order":
                                    if trades_dict[new_order["message_id"]].get("status") not in ["active", "closed", "cancelled", "filled"]:
                                        logger.info(" ⛏️ OKX place_new_order")
                                        available_info = trades_dict[new_order["message_id"]]["instrument_info"]
                                        ok = await okx_trade.place_new_order(trades_dict=trades_dict, instrument_id=okx_coin, trade_info=new_order, instrument_info=available_info)
                                        if ok:
                                            await send_message(f"📌 OKX: ордер по {coin} размещён")

                                elif action == "enter_now":
                                    if trades_dict[new_order["message_id"]].get("sl_info") is None:
                                        logger.info(" ⛏️ OKX enter_now")
                                        if len(trades_dict[new_order["message_id"]]["entry"]) == 1:
                                            await okx_trade.cancel_orders(trades_dict=trades_dict, instrument_id=okx_coin, ord_ids=[trades_dict[new_order["message_id"]]["entry_info"]["ordId"]])
                                        elif len(trades_dict[new_order["message_id"]]["entry"]) == 2:
                                            ord_ids = [trades_dict[new_order["message_id"]]["entry_info"][0]["ordId"], trades_dict[new_order["message_id"]]["entry_info"][1]["ordId"]]
                                            await okx_trade.cancel_orders(trades_dict=trades_dict, instrument_id=okx_coin, ord_ids=ord_ids)
                                        available_info = trades_dict[new_order["message_id"]]["instrument_info"]
                                        await okx_trade.enter_now(trades_dict=trades_dict, instrument_id=okx_coin, trade_info=new_order, instrument_info=available_info)

                                elif action == "stop_breakeven":
                                    if trades_dict[new_order["message_id"]].get("status") in ["active", "filled"]:
                                        if trades_dict[new_order["message_id"]].get("sl_info", None) is not None and trades_dict[new_order["message_id"]]["sl_info"].get("breakeven") is False:
                                            logger.info(" ⛏️ OKX stop_breakeven")
                                            await okx_trade.stop_breakeven(trades_dict=trades_dict, instrument_id=okx_coin, trade_info=new_order, instrument_info=available_info)

                                elif action == "cancel":
                                    if trades_dict[new_order["message_id"]].get("status") == "active":
                                        logger.info(" ⛏️ OKX cancel")
                                        ord_ids = []
                                        if len(trades_dict[new_order["message_id"]]["entry"]) == 1:
                                            ord_ids.append(trades_dict[new_order["message_id"]]["entry_info"]["ordId"])
                                        elif len(trades_dict[new_order["message_id"]]["entry"]) == 2:
                                            ord_ids.append(trades_dict[new_order["message_id"]]["entry_info"][0]["ordId"])
                                            ord_ids.append(trades_dict[new_order["message_id"]]["entry_info"][1]["ordId"])
                                        await okx_trade.cancel_orders(trades_dict=trades_dict, instrument_id=okx_coin, ord_ids=ord_ids)

                                elif action == "close_entire":
                                    if trades_dict[new_order["message_id"]].get("status") in ["filled", "partially_filled"]:
                                        logger.info(" ⛏️ OKX close_entire")
                                        available_info = trades_dict[new_order["message_id"]]["instrument_info"]
                                        ord_ids = [trades_dict[new_order["message_id"]]["sl_info"]["ordId"] if trades_dict[new_order["message_id"]].get("sl_info", None) else None]
                                        if len(trades_dict[new_order["message_id"]]["entry"]) == 2:
                                            ord_ids.append(trades_dict[new_order["message_id"]]["entry_info"][1]["ordId"])
                                        if trades_dict[new_order["message_id"]].get("tp_info", None) is not None:
                                            if len(trades_dict[new_order["message_id"]]["tp_info"]) == 1:
                                                ord_ids.append(trades_dict[new_order["message_id"]]["tp_info"]["ordId"])
                                            elif len(trades_dict[new_order["message_id"]]["tp_info"]) == 2:
                                                ord_ids.append(trades_dict[new_order["message_id"]]["tp_info"][0]["ordId"])
                                                ord_ids.append(trades_dict[new_order["message_id"]]["tp_info"][1]["ordId"])
                                            elif len(trades_dict[new_order["message_id"]]["tp_info"]) == 3:
                                                ord_ids.append(trades_dict[new_order["message_id"]]["tp_info"][0]["ordId"])
                                                ord_ids.append(trades_dict[new_order["message_id"]]["tp_info"][1]["ordId"])
                                                ord_ids.append(trades_dict[new_order["message_id"]]["tp_info"][2]["ordId"])
                                        res = await okx_trade.close_entire_position(trades_dict=trades_dict, instrument_id=okx_coin, trade_info=new_order, instrument_info=available_info)
                                        if res:
                                            await okx_trade.cancel_orders(trades_dict=trades_dict, instrument_id=okx_coin, ord_ids=ord_ids)
                                        else:
                                            logger.error(f"❌ {okx_coin} Error closing entire position okx_strategy_pool")

                                else:
                                    logger.warning(" ❌ Unsupported action: %s", action)

                            elif message_id in trades_dict and trades_dict[new_order["message_id"]]["exchange"] == "BYBIT":
                                await bybit_queue.put(new_order)

        try:
            ws_message = okx_ws_queue.get_nowait()
            # Process the message about orders
            data = ws_message.get("data", [])
            if not data:
                continue

            for item in data:
                order_id = item.get("ordId")
                instId = item.get("instId")
                if instId:
                    instId = instId.replace("-", "").replace("SWAP", "")
                algo_id = item.get("algoId")

                # Check if algo_id matches any trades' sl_info 'ordId'
                algo_key = None
                for key, trade in trades_dict.items():
                    sl_info = trade.get("sl_info", {})
                    if sl_info and sl_info.get("ordId") == algo_id:
                        algo_key = key
                        break
                if algo_key is not None:
                    trades_dict[algo_key]["sl_info"]["ordId"] = order_id

                # Ключ трейда по совпадению монеты (instId уже без дефисов: XRPUSDT)
                coin_key = next((k for k in trades_dict if trades_dict[k].get("coin") == instId), None)
                if coin_key is not None:
                    available_info = trades_dict[coin_key]["instrument_info"]
                    await ws_process(data=data, trades_dict=trades_dict, okx_trade=okx_trade, trade_info=new_order, instrument_info=available_info)
        except asyncio.QueueEmpty:
            pass

        await asyncio.sleep(0.05)


        