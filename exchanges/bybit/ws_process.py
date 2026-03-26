"""
Обработка WebSocket сообщений Bybit (order/position).
Формат: {"topic": "order"|"position", "data": [list of dicts]}
Поля order: orderId, symbol, orderStatus (New, PartiallyFilled, Filled, Cancelled),
  cumExecQty, avgPrice, qty, side, reduceOnly
"""
import asyncio

from loguru import logger

from exchanges.bybit.trade import BybitTrade
from tg_bot.notify import send_message

from pprint import pformat


def _pprint_to_log(obj) -> None:
    """Печатает pprint-дамп в loguru, чтобы его было видно в лог-файле."""
    logger.debug(pformat(obj, indent=2, width=80, sort_dicts=False))

def _find_trade_by_order_id(trades_dict: dict, order_id: str, mapped_symbol: str):
    """Найти трейд по orderId (entry / tp / sl). Логируем странные состояния вместо падения."""
    for msg_id, trade in trades_dict.items():
        if trade.get("coin") != mapped_symbol or trade.get("exchange") != "BYBIT":
            continue

        try:
            entry = trade.get("entry")
            entry_info = trade.get("entry_info")

            # Бывает, что entry уже None – логируем и не пытаемся брать len(None)
            if entry:
                if len(entry) == 1 and isinstance(entry_info, dict) and entry_info.get("ordId") == order_id:
                    return msg_id, trade
                if len(entry) == 2 and isinstance(entry_info, list) and len(entry_info) >= 2:
                    if entry_info[0].get("ordId") == order_id or entry_info[1].get("ordId") == order_id:
                        return msg_id, trade
            else:
                logger.debug(f"Bybit ws: trade {msg_id!r} has empty/None entry when matching orderId={order_id!r}: {trade!r}")

            tp = trade.get("tp_info")
            if tp is not None:
                if isinstance(tp, dict) and tp.get("ordId") == order_id:
                    return msg_id, trade
                if isinstance(tp, (list, tuple)):
                    for t in tp:
                        if isinstance(t, dict) and t.get("ordId") == order_id:
                            return msg_id, trade

            sl = trade.get("sl_info")
            if isinstance(sl, dict) and sl.get("ordId") == order_id:
                return msg_id, trade

        except Exception as e:
            logger.exception(f"Bybit ws: error in _find_trade_by_order_id for msg_id={msg_id!r}, order_id={order_id!r}, trade={trade!r}")
            continue

    return None, None


async def ws_process(data: list, trades_dict: dict, bybit_trade: BybitTrade):
    """
    Обработка order updates от Bybit WebSocket.
    data: список dict с полями orderId, symbol, orderStatus, cumExecQty, avgPrice, ...
    instrument_info берётся из trade["instrument_info"].
    """
    for item in data:
        order_id = item.get("orderId")
        symbol = item.get("symbol", "")
        order_status = item.get("orderStatus", "")
        cum_exec_qty = item.get("cumExecQty", "0")
        avg_price = item.get("avgPrice") or item.get("price") or "0"

        trade_key, trade = _find_trade_by_order_id(trades_dict, order_id, symbol)
        if trade_key is None:
            logger.debug(f"Bybit ws: no trade found for symbol={symbol!r} order_id={order_id!r} item={item!r}")
            continue
        effective_trade_info = trade
        instrument_info = trade.get("instrument_info")
        if not instrument_info:
            logger.warning(f"{symbol!r} Bybit ws_process: no instrument_info for trade {trade_key!r}")
            continue

        entry = trade.get("entry") if isinstance(trade.get("entry"), list) else []
        entry = [x for x in entry if x is not None]

        if len(entry) == 1:
            if trade["entry_info"]["ordId"] == order_id:
                if order_status in ("Cancelled", "Rejected"):
                    logger.info(f"✅ {symbol} ENTRY {order_status} order {order_id}")
                    trade["status"] = "cancelled"
                    trades_dict.pop(trade_key, None)
                    _pprint_to_log(trade)
                    continue

                elif order_status == "PartiallyFilled":
                    logger.info(f"✅ {symbol} ENTRY {order_status} order {order_id} fill size: {cum_exec_qty}")
                    trade["current_fill_size"] = float(cum_exec_qty or "0")
                    trade["entry_info"]["price_filled"] = avg_price
                    trade["entry_info"]["size_filled"] = cum_exec_qty
                    trade["entry_info"]["status"] = {order_status}
                    trade["status"] = "filled"

                if order_status == "Filled":
                    logger.info(f"✅ {symbol} ENTRY {order_status} order {order_id} fill size: {cum_exec_qty}")
                    trade["current_fill_size"] = float(cum_exec_qty or "0")
                    trade["entry_info"]["price_filled"] = avg_price
                    trade["entry_info"]["size_filled"] = cum_exec_qty
                    trade["entry_info"]["status"] = {order_status}
                    trade["status"] = "filled"

                    await send_message(f"✅ ENTRY {symbol} — filled")

                    if trade.get("sl_info", None) is not None:
                        await bybit_trade.cancel_orders(trades_dict=trades_dict, instrument_id=symbol, ord_ids=[trade["sl_info"]["ordId"]])
                    elif trade.get("tp_info", None) is not None:
                        tp_ids = []
                        if isinstance(trade["tp_info"], dict):
                            tp_ids.append(trade["tp_info"]["ordId"])
                        elif isinstance(trade["tp_info"], (list, tuple)):
                            for t in trade["tp_info"]:
                                if isinstance(t, dict) and t.get("ordId"):
                                    tp_ids.append(t["ordId"])
                        if tp_ids:
                            await bybit_trade.cancel_orders(trades_dict=trades_dict, instrument_id=symbol, ord_ids=tp_ids)
                    logger.info(f"trade {trade}")
                    await bybit_trade.place_stop_loss_order(trades_dict=trades_dict, instrument_id=symbol, trade_info=effective_trade_info, instrument_info=instrument_info)
                    await bybit_trade.place_tps_first_limit_order(trades_dict=trades_dict, instrument_id=symbol, trade_info=effective_trade_info, instrument_info=instrument_info)
                    _pprint_to_log(trade)

        elif len(entry) == 2:
            if trade["entry_info"][0]["ordId"] == order_id:
                if order_status in ("Cancelled", "Rejected"):
                    logger.info(f"✅ {symbol} ENTRY1 {order_status} order {order_id}")
                    trade["status"] = "cancelled"
                    trades_dict.pop(trade_key, None)
                    continue

                elif order_status == "PartiallyFilled":
                    logger.info(f"✅ {symbol} ENTRY1 {order_status} order {order_id} fill size: {cum_exec_qty}")
                    trade["current_fill_size"] = float(cum_exec_qty or "0")
                    trade["entry_info"][0]["price_filled"] = avg_price
                    trade["entry_info"][0]["size_filled"] = cum_exec_qty
                    trade["entry_info"][0]["status"] = {order_status}
                    trade["status"] = "filled"

                if order_status == "Filled":
                    logger.info(f"✅ {symbol} ENTRY1 {order_status} order {order_id} fill size: {cum_exec_qty}")
                    trade["current_fill_size"] = float(cum_exec_qty or "0")
                    trade["entry_info"][0]["price_filled"] = avg_price
                    trade["entry_info"][0]["size_filled"] = cum_exec_qty
                    trade["entry_info"][0]["status"] = {order_status}
                    trade["status"] = "filled"

                    await send_message(f"✅ ENTRY1 {symbol} — filled")

                    if trade.get("sl_info", None) is not None:
                        await bybit_trade.cancel_orders(trades_dict=trades_dict, instrument_id=symbol, ord_ids=[trade["sl_info"]["ordId"]])
                    elif trade.get("tp_info", None) is not None:
                        tp_ids = []
                        for t in (trade["tp_info"] if isinstance(trade["tp_info"], (list, tuple)) else [trade["tp_info"]]):
                            if isinstance(t, dict) and t.get("ordId"):
                                tp_ids.append(t["ordId"])
                        if tp_ids:
                            await bybit_trade.cancel_orders(trades_dict=trades_dict, instrument_id=symbol, ord_ids=tp_ids)
                    await bybit_trade.place_stop_loss_order(trades_dict=trades_dict, instrument_id=symbol, trade_info=effective_trade_info, instrument_info=instrument_info)
                    await bybit_trade.place_tps_first_limit_order(trades_dict=trades_dict, instrument_id=symbol, trade_info=effective_trade_info, instrument_info=instrument_info)
                    _pprint_to_log(trade)

            elif trade["entry_info"][1]["ordId"] == order_id:
                if order_status in ("Cancelled", "Rejected"):
                    logger.info(f"✅ {symbol} ENTRY2 {order_status} order {order_id}")
                    trade["status"] = "cancelled"
                    if trade.get("sl_info", None) is not None:
                        await bybit_trade.cancel_orders(trades_dict=trades_dict, instrument_id=symbol, ord_ids=[trade["sl_info"]["ordId"]])
                    elif trade.get("tp_info", None) is not None:
                        tp_ids = []
                        for t in (trade["tp_info"] if isinstance(trade["tp_info"], (list, tuple)) else [trade["tp_info"]]):
                            if isinstance(t, dict) and t.get("ordId"):
                                tp_ids.append(t["ordId"])
                        if tp_ids:
                            await bybit_trade.cancel_orders(trades_dict=trades_dict, instrument_id=symbol, ord_ids=tp_ids)
                    _pprint_to_log(trade)
                    trades_dict.pop(trade_key, None)
                    continue

                elif order_status == "PartiallyFilled":
                    logger.info(f"✅ {symbol} ENTRY2 {order_status} order {order_id} fill size: {cum_exec_qty}")
                    entry1_sz = float(trade["entry_info"][0].get("size_filled", 0) or 0)
                    trade["current_fill_size"] = entry1_sz + float(cum_exec_qty or "0")
                    trade["entry_info"][1]["price_filled"] = avg_price
                    trade["entry_info"][1]["size_filled"] = cum_exec_qty
                    trade["entry_info"][1]["status"] = {order_status}
                    trade["status"] = "filled"

                if order_status == "Filled":
                    logger.info(f"✅ {symbol} ENTRY2 {order_status} order {order_id} fill size: {cum_exec_qty}")
                    entry1_sz = float(trade["entry_info"][0].get("size_filled", 0) or 0)
                    trade["current_fill_size"] = entry1_sz + float(cum_exec_qty or "0")
                    trade["entry_info"][1]["price_filled"] = avg_price
                    trade["entry_info"][1]["size_filled"] = cum_exec_qty
                    trade["entry_info"][1]["status"] = {order_status}
                    trade["status"] = "filled"

                    await send_message(f"✅ ENTRY2 {symbol} — filled")

                    if trade.get("sl_info", None) is not None:
                        await bybit_trade.cancel_orders(trades_dict=trades_dict, instrument_id=symbol, ord_ids=[trade["sl_info"]["ordId"]])
                    elif trade.get("tp_info", None) is not None:
                        tp_ids = []
                        for t in (trade["tp_info"] if isinstance(trade["tp_info"], (list, tuple)) else [trade["tp_info"]]):
                            if isinstance(t, dict) and t.get("ordId"):
                                tp_ids.append(t["ordId"])
                        if tp_ids:
                            await bybit_trade.cancel_orders(trades_dict=trades_dict, instrument_id=symbol, ord_ids=tp_ids)
                    await bybit_trade.place_stop_loss_order(trades_dict=trades_dict, instrument_id=symbol, trade_info=effective_trade_info, instrument_info=instrument_info)
                    await bybit_trade.place_tps_second_limit_order(trades_dict=trades_dict, instrument_id=symbol, trade_info=effective_trade_info, instrument_info=instrument_info)
                    _pprint_to_log(trade)

        # TP handling
        if trade.get("tp_info", None) is not None:
            tp_info = trade["tp_info"]
            tp_list = tp_info if isinstance(tp_info, (list, tuple)) else [tp_info]

            if len(tp_list) == 1:
                if tp_list[0].get("ordId") == order_id:
                    if order_status in ("Cancelled", "Rejected"):
                        logger.info(f"✅ {symbol} TP {order_status} order {order_id}")
                        del trade["tp_info"]
                    elif order_status == "PartiallyFilled":
                        logger.info(f"✅ {symbol} TP {order_status} order {order_id} fill size: {cum_exec_qty}")
                        trade["current_fill_size"] -= float(cum_exec_qty or "0")
                        tp_list[0]["price_filled"] = avg_price
                        tp_list[0]["size_filled"] = cum_exec_qty
                        tp_list[0]["status"] = {order_status}
                    elif order_status == "Filled":
                        logger.info(f"✅ {symbol} TP {order_status} order {order_id} fill size: {cum_exec_qty}")
                        trade["current_fill_size"] -= float(cum_exec_qty or "0")
                        tp_list[0]["price_filled"] = avg_price
                        tp_list[0]["size_filled"] = cum_exec_qty
                        tp_list[0]["status"] = {order_status}
                        if trade.get("sl_info", None) is not None:
                            await bybit_trade.cancel_orders(trades_dict=trades_dict, instrument_id=symbol, ord_ids=[trade["sl_info"]["ordId"]])
                        logger.info(f"✅✅✅ {symbol} closes all position by TP")
                        trade["status"] = "closed"
                        await send_message(f"✅ TP {symbol} — closed by TP")
                        trades_dict.pop(trade_key, None)
                        continue

            elif len(tp_list) == 2:
                if tp_list[0].get("ordId") == order_id:
                    if order_status in ("Cancelled", "Rejected"):
                        logger.info(f"✅ {symbol} TP1 {order_status} order {order_id}")
                        del trade["tp_info"]
                    elif order_status == "PartiallyFilled":
                        logger.info(f"✅ {symbol} TP1 {order_status} order {order_id} fill size: {cum_exec_qty}")
                        trade["current_fill_size"] -= float(cum_exec_qty or "0")
                        tp_list[0]["price_filled"] = avg_price
                        tp_list[0]["size_filled"] = cum_exec_qty
                        tp_list[0]["status"] = {order_status}
                    elif order_status == "Filled":
                        logger.info(f"✅ {symbol} TP1 {order_status} order {order_id} fill size: {cum_exec_qty}")
                        trade["current_fill_size"] -= float(cum_exec_qty or "0")
                        tp_list[0]["price_filled"] = avg_price
                        tp_list[0]["size_filled"] = cum_exec_qty
                        tp_list[0]["status"] = {order_status}
                        if trade.get("sl_info", None) is not None:
                            await bybit_trade.cancel_orders(trades_dict=trades_dict, instrument_id=symbol, ord_ids=[trade["sl_info"]["ordId"]])
                        await bybit_trade.place_stop_loss_order(trades_dict=trades_dict, instrument_id=symbol, trade_info=effective_trade_info, instrument_info=instrument_info)
                        _pprint_to_log(trade)
                        await send_message(f"✅ TP1 {symbol} — filled")

                elif tp_list[1].get("ordId") == order_id:
                    if order_status in ("Cancelled", "Rejected"):
                        logger.info(f"✅ {symbol} TP2 {order_status} order {order_id}")
                        del trade["tp_info"]
                    elif order_status == "PartiallyFilled":
                        logger.info(f"✅ {symbol} TP2 {order_status} order {order_id} fill size: {cum_exec_qty}")
                        trade["current_fill_size"] -= float(cum_exec_qty or "0")
                        tp_list[1]["price_filled"] = avg_price
                        tp_list[1]["size_filled"] = cum_exec_qty
                        tp_list[1]["status"] = {order_status}
                    elif order_status == "Filled":
                        logger.info(f"✅ {symbol} TP2 {order_status} order {order_id} fill size: {cum_exec_qty}")
                        trade["current_fill_size"] -= float(cum_exec_qty or "0")
                        tp_list[1]["price_filled"] = avg_price
                        tp_list[1]["size_filled"] = cum_exec_qty
                        tp_list[1]["status"] = {order_status}
                        if trade.get("sl_info", None) is not None:
                            await bybit_trade.cancel_orders(trades_dict=trades_dict, instrument_id=symbol, ord_ids=[trade["sl_info"]["ordId"]])
                        logger.info(f"✅✅✅ {symbol} closes all position by TP")
                        trade["status"] = "closed"
                        await send_message(f"✅ TP2 {symbol} — closed by TP")
                        trades_dict.pop(trade_key, None)
                        continue

            elif len(tp_list) == 3:
                if tp_list[0].get("ordId") == order_id:
                    if order_status in ("Cancelled", "Rejected"):
                        logger.info(f"✅ {symbol} TP1 {order_status} order {order_id}")
                        del trade["tp_info"]
                    elif order_status == "PartiallyFilled":
                        logger.info(f"✅ {symbol} TP1 {order_status} order {order_id} fill size: {cum_exec_qty}")
                        trade["current_fill_size"] -= float(cum_exec_qty or "0")
                        tp_list[0]["price_filled"] = avg_price
                        tp_list[0]["size_filled"] = cum_exec_qty
                        tp_list[0]["status"] = {order_status}
                    elif order_status == "Filled":
                        logger.info(f"✅ {symbol} TP1 {order_status} order {order_id} fill size: {cum_exec_qty}")
                        trade["current_fill_size"] -= float(cum_exec_qty or "0")
                        tp_list[0]["price_filled"] = avg_price
                        tp_list[0]["size_filled"] = cum_exec_qty
                        tp_list[0]["status"] = {order_status}
                        if trade.get("sl_info", None) is not None:
                            await bybit_trade.cancel_orders(trades_dict=trades_dict, instrument_id=symbol, ord_ids=[trade["sl_info"]["ordId"]])
                        await bybit_trade.place_stop_loss_order(trades_dict=trades_dict, instrument_id=symbol, trade_info=effective_trade_info, instrument_info=instrument_info)
                        await bybit_trade.stop_breakeven(trades_dict=trades_dict, instrument_id=symbol, trade_info=effective_trade_info, instrument_info=instrument_info)
                        _pprint_to_log(trade)
                        await send_message(f"✅ TP1 {symbol} — filled")

                elif tp_list[1].get("ordId") == order_id:
                    if order_status in ("Cancelled", "Rejected"):
                        logger.info(f"✅ {symbol} TP2 {order_status} order {order_id}")
                        del trade["tp_info"]
                    elif order_status == "PartiallyFilled":
                        logger.info(f"✅ {symbol} TP2 {order_status} order {order_id} fill size: {cum_exec_qty}")
                        trade["current_fill_size"] -= float(cum_exec_qty or "0")
                        tp_list[1]["price_filled"] = avg_price
                        tp_list[1]["size_filled"] = cum_exec_qty
                        tp_list[1]["status"] = {order_status}
                    elif order_status == "Filled":
                        logger.info(f"✅ {symbol} TP2 {order_status} order {order_id} fill size: {cum_exec_qty}")
                        trade["current_fill_size"] -= float(cum_exec_qty or "0")
                        tp_list[1]["price_filled"] = avg_price
                        tp_list[1]["size_filled"] = cum_exec_qty
                        tp_list[1]["status"] = {order_status}
                        if trade.get("sl_info", None) is not None:
                            await bybit_trade.cancel_orders(trades_dict=trades_dict, instrument_id=symbol, ord_ids=[trade["sl_info"]["ordId"]])
                        await bybit_trade.place_stop_loss_order(trades_dict=trades_dict, instrument_id=symbol, trade_info=effective_trade_info, instrument_info=instrument_info)
                        await bybit_trade.stop_breakeven(trades_dict=trades_dict, instrument_id=symbol, trade_info=effective_trade_info, instrument_info=instrument_info)
                        _pprint_to_log(trade)
                        await send_message(f"✅ TP2 {symbol} — filled")

                elif tp_list[2].get("ordId") == order_id:
                    if order_status in ("Cancelled", "Rejected"):
                        logger.info(f"✅ {symbol} TP3 {order_status} order {order_id}")
                        del trade["tp_info"]
                    elif order_status == "PartiallyFilled":
                        logger.info(f"✅ {symbol} TP3 {order_status} order {order_id} fill size: {cum_exec_qty}")
                        trade["current_fill_size"] -= float(cum_exec_qty or "0")
                        tp_list[2]["price_filled"] = avg_price
                        tp_list[2]["size_filled"] = cum_exec_qty
                        tp_list[2]["status"] = {order_status}
                    elif order_status == "Filled":
                        logger.info(f"✅ {symbol} TP3 {order_status} order {order_id} fill size: {cum_exec_qty}")
                        trade["current_fill_size"] -= float(cum_exec_qty or "0")
                        tp_list[2]["price_filled"] = avg_price
                        tp_list[2]["size_filled"] = cum_exec_qty
                        tp_list[2]["status"] = {order_status}
                        if trade.get("sl_info", None) is not None:
                            await bybit_trade.cancel_orders(trades_dict=trades_dict, instrument_id=symbol, ord_ids=[trade["sl_info"]["ordId"]])
                        logger.info(f"✅✅✅ {symbol} closes all position by TP")
                        trade["status"] = "closed"
                        await send_message(f"✅ TP {symbol} — closed by TP")
                        _pprint_to_log(trade)
                        trades_dict.pop(trade_key, None)
                        continue

        # SL handling
        if trade.get("sl_info", None) is not None:
            if trade["sl_info"]["ordId"] == order_id:
                if order_status in ("Cancelled", "Rejected"):
                    logger.info(f"✅ {symbol} SL {order_status} order {order_id}")
                    del trade["sl_info"]
                elif order_status == "PartiallyFilled":
                    logger.info(f"✅ {symbol} SL {order_status} order {order_id} fill size: {cum_exec_qty}")
                    trade["current_fill_size"] -= float(cum_exec_qty or "0")
                    trade["sl_info"]["price_filled"] = avg_price
                    trade["sl_info"]["size_filled"] = cum_exec_qty
                    trade["sl_info"]["status"] = {order_status}
                elif order_status == "Filled":
                    logger.info(f"✅ {symbol} SL {order_status} order {order_id} fill size: {cum_exec_qty}")
                    trade["current_fill_size"] -= float(cum_exec_qty or "0")
                    trade["sl_info"]["price_filled"] = avg_price
                    trade["sl_info"]["size_filled"] = cum_exec_qty
                    trade["sl_info"]["status"] = {order_status}
                    if trade.get("tp_info", None) is not None:
                        tp_ids = []
                        for t in (trade["tp_info"] if isinstance(trade["tp_info"], (list, tuple)) else [trade["tp_info"]]):
                            if isinstance(t, dict) and t.get("ordId"):
                                tp_ids.append(t["ordId"])
                        if tp_ids:
                            await bybit_trade.cancel_orders(trades_dict=trades_dict, instrument_id=symbol, ord_ids=tp_ids)
                    logger.info(f"✅✅✅ {symbol} closes all position by SL")
                    trades_dict[effective_trade_info["message_id"]]["status"] = "closed"
                    await send_message(f"🛑 SL {symbol} — closed by SL")
                    _pprint_to_log(trade)
                    trades_dict.pop(trade_key, None)
                    continue
