from asyncio import Queue
import asyncio
import logging
import os as _os

from config import BALANCE
from exchanges.bybit.trade import BybitTrade
from exchanges.bybit.ws_process import ws_process
from tg_bot.notify import send_message

_logs_dir = _os.path.normpath(_os.path.join(_os.path.dirname(__file__), "..", "..", "logs"))
_os.makedirs(_logs_dir, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(_os.path.join(_logs_dir, "logarik.log"), encoding="utf-8"),
        logging.StreamHandler(),
    ],
)


async def bybit_strategy_pool(bybit_queue: Queue, trades_dict: dict, bybit_ws_queue: Queue = None, bybit_trade: BybitTrade = None):
    """
    Основной цикл обработки ордеров для Bybit.
    - Берём JSON из очереди bybit_queue (ордера, которые не доступны на OKX)
    - Обрабатываем WS сообщения из bybit_ws_queue
    - Структура и логика как у okx_strategy_pool
    """
    if bybit_trade is None:
        api_key = _os.environ.get("BYBIT_API_KEY", "")
        api_secret = _os.environ.get("BYBIT_API_SECRET", "")
        bybit_trade = BybitTrade(api_key=api_key, api_secret=api_secret, balance=BALANCE)

    while True:
        # 1. Обработка ордеров из bybit_queue
        try:
            new_order = bybit_queue.get_nowait()
        except asyncio.QueueEmpty:
            new_order = None

        if new_order is not None:
            logging.info(f" ✅ bybit_strategy_pool: new_order: {new_order}")
            bybit_coin = new_order.get("coin")
            if bybit_coin:
                bybit_coin = bybit_coin.upper()
                new_order["coin"] = bybit_coin
            action = new_order.get("action")
            message_id = new_order.get("message_id")

            if not bybit_coin:
                if message_id in trades_dict:
                    bybit_coin = trades_dict[message_id].get("coin")
                if not bybit_coin:
                    logging.warning(" ❌ Bybit: missing coin and message_id not in trades_dict: %r", new_order)
                    bybit_coin = None

            if bybit_coin:
                if message_id not in trades_dict:
                    available_info = await bybit_trade.get_instrument_info(bybit_coin)
                    if available_info[0] is None:
                        logging.warning("%s not available on Bybit", bybit_coin)
                    else:
                        new_order["exchange"] = "BYBIT"
                        trades_dict[message_id] = new_order.copy()
                        trades_dict[message_id]["instrument_info"] = available_info

                if message_id in trades_dict and trades_dict[message_id].get("exchange") == "BYBIT":
                    available_info = trades_dict[message_id]["instrument_info"]
                    symbol = bybit_coin  # Bybit: UNIUSDT

                    if action == "place_new_order":
                        if trades_dict[message_id].get("status") not in ["active", "closed", "cancelled", "filled"]:
                            logging.info(" ⛏️ BYBIT place_new_order")
                            ok = await bybit_trade.place_new_order(trades_dict=trades_dict, instrument_id=symbol, trade_info=new_order, instrument_info=available_info)
                            if ok:
                                await send_message(f"📌 Bybit: ордер по {bybit_coin} размещён")

                    elif action == "enter_now":
                        if trades_dict[message_id].get("sl_info") is None:
                            logging.info(" ⛏️ BYBIT enter_now")
                            entry_info = trades_dict[message_id].get("entry_info")
                            if entry_info:
                                if isinstance(entry_info, dict):
                                    await bybit_trade.cancel_orders(trades_dict=trades_dict, instrument_id=symbol, ord_ids=[entry_info["ordId"]])
                                elif isinstance(entry_info, list) and len(entry_info) >= 2:
                                    await bybit_trade.cancel_orders(trades_dict=trades_dict, instrument_id=symbol, ord_ids=[entry_info[0]["ordId"], entry_info[1]["ordId"]])
                                elif isinstance(entry_info, list) and entry_info:
                                    await bybit_trade.cancel_orders(trades_dict=trades_dict, instrument_id=symbol, ord_ids=[entry_info[0]["ordId"]])
                            await bybit_trade.enter_now(trades_dict=trades_dict, instrument_id=symbol, trade_info=new_order, instrument_info=available_info)

                    elif action == "stop_breakeven":
                        if trades_dict[message_id].get("status") in ["active", "filled"]:
                            if trades_dict[message_id].get("sl_info") and trades_dict[message_id]["sl_info"].get("breakeven") is False:
                                logging.info(" ⛏️ BYBIT stop_breakeven")
                                await bybit_trade.stop_breakeven(trades_dict=trades_dict, instrument_id=symbol, trade_info=new_order, instrument_info=available_info)

                    elif action == "cancel":
                        if trades_dict[message_id].get("status") == "active":
                            logging.info(" ⛏️ BYBIT cancel")
                            ord_ids = []
                            entry_info = trades_dict[message_id].get("entry_info")
                            if isinstance(entry_info, dict):
                                ord_ids.append(entry_info["ordId"])
                            elif isinstance(entry_info, list) and len(entry_info) >= 2:
                                ord_ids.extend([entry_info[0]["ordId"], entry_info[1]["ordId"]])
                            elif isinstance(entry_info, list) and entry_info:
                                ord_ids.append(entry_info[0]["ordId"])
                            if ord_ids:
                                await bybit_trade.cancel_orders(trades_dict=trades_dict, instrument_id=symbol, ord_ids=ord_ids)

                    elif action == "close_entire":
                        if trades_dict[message_id].get("status") in ["filled", "partially_filled"]:
                            logging.info(" ⛏️ BYBIT close_entire")
                            ord_ids = []
                            sl_info = trades_dict[message_id].get("sl_info")
                            if sl_info and sl_info.get("ordId") != "bybit_position_sl":
                                ord_ids.append(sl_info["ordId"])
                            entry_info = trades_dict[message_id].get("entry_info")
                            if isinstance(entry_info, list) and len(entry_info) >= 2:
                                ord_ids.append(entry_info[1]["ordId"])
                            tp_info = trades_dict[message_id].get("tp_info")
                            if tp_info:
                                if isinstance(tp_info, dict):
                                    ord_ids.append(tp_info["ordId"])
                                elif isinstance(tp_info, list):
                                    for t in tp_info:
                                        if isinstance(t, dict) and t.get("ordId"):
                                            ord_ids.append(t["ordId"])
                            res = await bybit_trade.close_entire_position(trades_dict=trades_dict, instrument_id=symbol, trade_info=new_order, instrument_info=available_info)
                            if res and ord_ids:
                                await bybit_trade.cancel_orders(trades_dict=trades_dict, instrument_id=symbol, ord_ids=ord_ids)

                    elif action in ("info", "help"):
                        logging.info(" ⛏️ BYBIT %s - skip", action)
                    else:
                        logging.warning(" ❌ Unsupported action: %s", action)

        # 2. Обработка WS сообщений
        if bybit_ws_queue is not None:
            try:
                ws_message = bybit_ws_queue.get_nowait()
            except asyncio.QueueEmpty:
                ws_message = None

            if ws_message is not None:
                topic = ws_message.get("topic")
                data = ws_message.get("data", [])
                if not data:
                    pass
                elif topic == "order":
                    await ws_process(data=data, trades_dict=trades_dict, bybit_trade=bybit_trade)

        await asyncio.sleep(0.05)
