import logging
import asyncio

from exchanges.okx.trade import OkxTrade
from tg_bot.notify import send_message

from pprint import pprint


def _find_trade_by_order_id(trades_dict: dict, order_id: str, mapped_symbol: str):
    """Найти трейд по ordId (entry / tp / sl). При нескольких сетапах по одной монете — нужный."""
    for msg_id, trade in trades_dict.items():
        if trade.get("coin") != mapped_symbol:
            continue
        entry = trade.get("entry", [])
        entry_info = trade.get("entry_info")
        if len(entry) == 1 and isinstance(entry_info, dict) and entry_info.get("ordId") == order_id:
            return msg_id, trade
        if len(entry) == 2 and isinstance(entry_info, list) and len(entry_info) >= 2:
            if entry_info[0].get("ordId") == order_id or entry_info[1].get("ordId") == order_id:
                return msg_id, trade
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
    return None, None


async def ws_process(data: dict, trades_dict: dict, okx_trade: OkxTrade, trade_info: dict, instrument_info: tuple):
    # Process the message about orders

    for item in data:
        order_id = item.get("ordId")
        instId = item.get("instId")
        mapped_symbol = instId.replace("-", "").replace("SWAP", "")
        state = item.get("state")

        # Ключ трейда по ordId (entry/tp/sl), а не по монете — чтобы при двух сетапах по одной паре брать нужный
        trade_key, trade = _find_trade_by_order_id(trades_dict, order_id, mapped_symbol)
        if trade_key is None:
            continue
        effective_trade_info = trade  # для place_* нужен message_id именно этого трейда

        if len(trade["entry"]) == 1:
            if trade["entry_info"]["ordId"] == order_id:
                if state == "canceled":
                    logging.info(f"✅ {instId} ENTRY {state} order {order_id}")
                    trade["status"] = "cancelled"
                    pprint(trade, indent=2, width=80, sort_dicts=False)

                
                elif state == "partially_filled":
                    logging.info(f"✅ {instId} ENTRY {state} order {order_id} fill size: {item.get('accFillSz')}")
                    trade["current_fill_size"] = float(item.get("accFillSz"))
                    trade["entry_info"]["price_filled"] = item.get("fillPx")
                    trade["entry_info"]["size_filled"] = item.get("accFillSz")
                    trade["entry_info"]["status"] = {state}
                    trade["status"] = "filled"

                if state == "filled":
                    logging.info(f"✅ {instId} ENTRY {state} order {order_id} fill size: {item.get('accFillSz')}")
                    trade["current_fill_size"] = float(item.get("accFillSz"))
                    trade["entry_info"]["price_filled"] = item.get("fillPx")
                    trade["entry_info"]["size_filled"] = item.get("accFillSz")
                    trade["entry_info"]["status"] = {state}
                    trade["status"] = "filled"

                    # Уведомление в ТГ о том, что вход исполнен 
                    await send_message(f"✅ ENTRY {instId} — filled")
                    
                    if trade.get("sl_info", None) is not None:
                        await okx_trade.cancel_algo_orders(trades_dict=trades_dict, instrument_id=instId, algo_ids=[trade["sl_info"]["ordId"]])
                    elif trade.get("tp_info", None) is not None:
                        tp_ids = []
                        if len(trade["tp_info"]) == 1:
                            tp_ids.append(trade["tp_info"]["ordId"])
                        elif len(trade["tp_info"]) == 2:
                            tp_ids.append(trade["tp_info"][0]["ordId"])
                            tp_ids.append(trade["tp_info"][1]["ordId"])
                        elif len(trade["tp_info"]) == 3:
                            tp_ids.append(trade["tp_info"][0]["ordId"])
                            tp_ids.append(trade["tp_info"][1]["ordId"])
                            tp_ids.append(trade["tp_info"][2]["ordId"])
                        await okx_trade.cancel_orders(trades_dict=trades_dict, instrument_id=instId, ord_ids=tp_ids)
                    logging.info(f"trade {trade}")
                    await okx_trade.place_stop_loss_order(trades_dict=trades_dict, instrument_id=instId, trade_info=effective_trade_info, instrument_info=instrument_info)
                    await okx_trade.place_tps_first_limit_order(trades_dict=trades_dict, instrument_id=instId, trade_info=effective_trade_info, instrument_info=instrument_info)
                    pprint(trade, indent=2, width=80, sort_dicts=False)
                                    
        elif len(trade["entry"]) == 2:

            if trade["entry_info"][0]["ordId"] == order_id:

                if state == "canceled":
                    logging.info(f"✅ {instId} ENTRY1{state} order {order_id}")
                    trade["status"] = "cancelled"
                    # if trade.get("sl_info", None) is not None:
                    #     await okx_trade.cancel_algo_orders(trades_dict=trades_dict, instrument_id=instId, algo_ids=[trade["sl_info"]["ordId"]])
                    
                    # elif trade.get("tp_info", None) is not None:
                    #     tp_ids = []
                    #     if len(trade["tp_info"]) == 1:
                    #         tp_ids.append(trade["tp_info"]["ordId"])
                    #     elif len(trade["tp_info"]) == 2:
                    #         tp_ids.append(trade["tp_info"][0]["ordId"])
                    #         tp_ids.append(trade["tp_info"][1]["ordId"])
                    #     elif len(trade["tp_info"]) == 3:
                    #         tp_ids.append(trade["tp_info"][0]["ordId"])
                    #         tp_ids.append(trade["tp_info"][1]["ordId"])
                    #         tp_ids.append(trade["tp_info"][2]["ordId"])
                    #     await okx_trade.cancel_orders(trades_dict=trades_dict, instrument_id=instId, ord_ids=tp_ids)
                                    
                elif state == "partially_filled":
                    logging.info(f"✅ {instId} ENTRY1 {state} order {order_id} fill size: {item.get('accFillSz')}")
                    trade["current_fill_size"] = float(item.get("accFillSz"))
                    trade["entry_info"][0]["price_filled"] = item.get("fillPx")
                    trade["entry_info"][0]["size_filled"] = item.get("accFillSz")
                    trade["entry_info"][0]["status"] = {state}
                    trade["status"] = "filled"

                if state == "filled":
                    logging.info(f"✅ {instId} ENTRY1 {state} order {order_id} fill size: {item.get('accFillSz')}")
                    trade["current_fill_size"] = float(item.get("accFillSz"))
                    trade["entry_info"][0]["price_filled"] = item.get("fillPx")
                    trade["entry_info"][0]["size_filled"] = item.get("accFillSz")
                    trade["entry_info"][0]["status"] = {state}
                    trade["status"] = "filled"

                    await send_message(f"✅ ENTRY1 {instId} — filled")

                    if trade.get("sl_info", None) is not None:
                        await okx_trade.cancel_algo_orders(trades_dict=trades_dict, instrument_id=instId, algo_ids=[trade["sl_info"]["ordId"]])
                    elif trade.get("tp_info", None) is not None:
                        tp_ids = []
                        if len(trade["tp_info"]) == 1:
                            tp_ids.append(trade["tp_info"]["ordId"])
                        elif len(trade["tp_info"]) == 2:
                            tp_ids.append(trade["tp_info"][0]["ordId"])
                            tp_ids.append(trade["tp_info"][1]["ordId"])
                        elif len(trade["tp_info"]) == 3:
                            tp_ids.append(trade["tp_info"][0]["ordId"])
                            tp_ids.append(trade["tp_info"][1]["ordId"])
                            tp_ids.append(trade["tp_info"][2]["ordId"])
                        await okx_trade.cancel_orders(trades_dict=trades_dict, instrument_id=instId, ord_ids=tp_ids)
                    await okx_trade.place_stop_loss_order(trades_dict=trades_dict, instrument_id=instId, trade_info=effective_trade_info, instrument_info=instrument_info)
                    await okx_trade.place_tps_first_limit_order(trades_dict=trades_dict, instrument_id=instId, trade_info=effective_trade_info, instrument_info=instrument_info)
                    pprint(trade, indent=2, width=80, sort_dicts=False)
            
            elif trade["entry_info"][1]["ordId"] == order_id:

                if state == "canceled":
                    logging.info(f"✅ {instId} ENTRY2 {state} order {order_id}")
                    trade["status"] = "cancelled"

                    if trade.get("sl_info", None) is not None:
                        await okx_trade.cancel_algo_orders(trades_dict=trades_dict, instrument_id=instId, algo_ids=[trade["sl_info"]["ordId"]])
                    
                    elif trade.get("tp_info", None) is not None:
                        tp_ids = []
                        if len(trade["tp_info"]) == 1:
                            tp_ids.append(trade["tp_info"]["ordId"])
                        elif len(trade["tp_info"]) == 2:
                            tp_ids.append(trade["tp_info"][0]["ordId"])
                            tp_ids.append(trade["tp_info"][1]["ordId"])
                        elif len(trade["tp_info"]) == 3:
                            tp_ids.append(trade["tp_info"][0]["ordId"])
                            tp_ids.append(trade["tp_info"][1]["ordId"])
                            tp_ids.append(trade["tp_info"][2]["ordId"])
                        await okx_trade.cancel_orders(trades_dict=trades_dict, instrument_id=instId, ord_ids=tp_ids)
                        pprint(trade, indent=2, width=80, sort_dicts=False)

                elif state == "partially_filled":
                    logging.info(f"✅ {instId} ENTRY2 {state} order {order_id} fill size: {item.get('accFillSz')}")
                    trade["current_fill_size"] = float(item.get("accFillSz"))
                    trade["entry_info"][1]["price_filled"] = item.get("fillPx")
                    trade["entry_info"][1]["size_filled"] = item.get("accFillSz")
                    trade["entry_info"][1]["status"] = {state}
                    trade["status"] = "filled"

                if state == "filled":
                    logging.info(f"✅ {instId} ENTRY2 {state} order {order_id} fill size: {item.get('accFillSz')}")
                    trade["current_fill_size"] = float(item.get("accFillSz"))
                    trade["entry_info"][1]["price_filled"] = item.get("fillPx")
                    trade["entry_info"][1]["size_filled"] = item.get("accFillSz")
                    trade["entry_info"][1]["status"] = {state}
                    trade["status"] = "filled"

                    await send_message(f"✅ ENTRY2 {instId} — filled")

                    if trade.get("sl_info", None) is not None:
                        await okx_trade.cancel_algo_orders(trades_dict=trades_dict, instrument_id=instId, algo_ids=[trade["sl_info"]["ordId"]])
                    elif trade.get("tp_info", None) is not None:
                        tp_ids = []
                        if len(trade["tp_info"]) == 1:
                            tp_ids.append(trade["tp_info"]["ordId"])
                        elif len(trade["tp_info"]) == 2:
                            tp_ids.append(trade["tp_info"][0]["ordId"])
                            tp_ids.append(trade["tp_info"][1]["ordId"])
                        elif len(trade["tp_info"]) == 3:
                            tp_ids.append(trade["tp_info"][0]["ordId"])
                            tp_ids.append(trade["tp_info"][1]["ordId"])
                            tp_ids.append(trade["tp_info"][2]["ordId"])
                        await okx_trade.cancel_orders(trades_dict=trades_dict, instrument_id=instId, ord_ids=tp_ids)
                    await okx_trade.place_stop_loss_order(trades_dict=trades_dict, instrument_id=instId, trade_info=effective_trade_info, instrument_info=instrument_info)
                    await okx_trade.place_tps_second_limit_order(trades_dict=trades_dict, instrument_id=instId, trade_info=effective_trade_info, instrument_info=instrument_info)
                    pprint(trade, indent=2, width=80, sort_dicts=False)
        
        if trade.get("tp_info", None) is not None:

            if len(trade["tp_info"]) == 1:
                if trade["tp_info"]["ordId"] == order_id:

                    if state == "canceled":
                        logging.info(f"✅ {instId} TP {state} order {order_id}")
                        del trade["tp_info"]

                    elif state == "partially_filled":
                        logging.info(f"✅ {instId} TP {state} order {order_id} fill size: {item.get('accFillSz')}")
                        trade["current_fill_size"] -= float(item.get("accFillSz"))
                        trade["tp_info"]["price_filled"] = item.get("fillPx")
                        trade["tp_info"]["size_filled"] = item.get("accFillSz")
                        trade["tp_info"]["status"] = {state}

                        
                    elif state == "filled":
                        logging.info(f"✅ {instId} TP {state} order {order_id} fill size: {item.get('accFillSz')}")
                        trade["current_fill_size"] -= float(item.get("accFillSz"))
                        trade["tp_info"]["price_filled"] = item.get("fillPx")
                        trade["tp_info"]["size_filled"] = item.get("accFillSz")
                        trade["tp_info"]["status"] = {state}
                        if trade.get("sl_info", None) is not None:
                            await okx_trade.cancel_algo_orders(trades_dict=trades_dict, instrument_id=instId, algo_ids=[trade["sl_info"]["ordId"]])
                        logging.info(f"✅✅✅ {instId} closes all position by TP")
                        trade["status"] = "closed"
                        await send_message(f"✅ TP {instId} — closed by TP")
                else:
                    logging.info(f'{instId} {order_id} not found in tp_info len: 1')

            elif len(trade["tp_info"]) == 2:

                if trade["tp_info"][0]["ordId"] == order_id:

                    if state == "canceled":
                        logging.info(f"✅ {instId} TP1 {state} order {order_id}")
                        del trade["tp_info"]

                    elif state == "partially_filled":
                        logging.info(f"✅ {instId} TP1 {state} order {order_id} fill size: {item.get('accFillSz')}")
                        trade["current_fill_size"] -= float(item.get("accFillSz"))
                        trade["tp_info"][0]["price_filled"] = item.get("fillPx")
                        trade["tp_info"][0]["size_filled"] = item.get("accFillSz")
                        trade["tp_info"][0]["status"] = {state}
                

                    elif state == "filled":
                        logging.info(f"✅ {instId} TP1 {state} order {order_id} fill size: {item.get('accFillSz')}")
                        trade["current_fill_size"] -= float(item.get("accFillSz"))
                        trade["tp_info"][0]["price_filled"] = item.get("fillPx")
                        trade["tp_info"][0]["size_filled"] = item.get("accFillSz")
                        trade["tp_info"][0]["status"] = {state}
                        if trade.get("sl_info", None) is not None:
                            await okx_trade.cancel_algo_orders(trades_dict=trades_dict, instrument_id=instId, algo_ids=[trade["sl_info"]["ordId"]])
                        await okx_trade.place_stop_loss_order(trades_dict=trades_dict, instrument_id=instId, trade_info=effective_trade_info, instrument_info=instrument_info)
                        pprint(trade, indent=2, width=80, sort_dicts=False)
                        await send_message(f"✅ TP1 {instId} — filled")

                elif trade["tp_info"][1]["ordId"] == order_id:
                    
                    if state == "canceled":
                        logging.info(f"✅ {instId} TP2 {state} order {order_id}")
                        del trade["tp_info"]

                    elif state == "partially_filled":
                        logging.info(f"✅ {instId} TP2 {state} order {order_id} fill size: {item.get('accFillSz')}")
                        trade["current_fill_size"] -= float(item.get("accFillSz"))
                        trade["tp_info"][1]["price_filled"] = item.get("fillPx")
                        trade["tp_info"][1]["size_filled"] = item.get("accFillSz")
                        trade["tp_info"][1]["status"] = {state}
                      

                    elif state == "filled":
                        logging.info(f"✅ {instId} TP2 {state} order {order_id} fill size: {item.get('accFillSz')}")
                        trade["current_fill_size"] -= float(item.get("accFillSz"))
                        trade["tp_info"][1]["price_filled"] = item.get("fillPx")
                        trade["tp_info"][1]["size_filled"] = item.get("accFillSz")
                        trade["tp_info"][1]["status"] = {state}
                        if trade.get("sl_info", None) is not None:
                            await okx_trade.cancel_algo_orders(trades_dict=trades_dict, instrument_id=instId, algo_ids=[trade["sl_info"]["ordId"]])
                        logging.info(f"✅✅✅ {instId} closes all position by TP")
                        trade["status"] = "closed"
                        await send_message(f"✅ TP2 {instId} — closed by TP")

            elif len(trade["tp_info"]) == 3:
                
                if trade["tp_info"][0]["ordId"] == order_id:

                    if state == "canceled":
                        logging.info(f"✅ {instId} TP1{state} order {order_id}")
                        del trade["tp_info"]

                    elif state == "partially_filled":
                        logging.info(f"✅ {instId} TP1 {state} order {order_id} fill size: {item.get('accFillSz')}")
                        trade["current_fill_size"] -= float(item.get("accFillSz"))
                        trade["tp_info"][0]["price_filled"] = item.get("fillPx")
                        trade["tp_info"][0]["size_filled"] = item.get("accFillSz")
                        trade["tp_info"][0]["status"] = {state}
                      
                        
                    elif state == "filled":
                        logging.info(f"✅ {instId} TP1 {state} order {order_id} fill size: {item.get('accFillSz')}")
                        trade["current_fill_size"] -= float(item.get("accFillSz"))
                        trade["tp_info"][0]["price_filled"] = item.get("fillPx")
                        trade["tp_info"][0]["size_filled"] = item.get("accFillSz")
                        trade["tp_info"][0]["status"] = {state}
                        if trade.get("sl_info", None) is not None:
                            await okx_trade.cancel_algo_orders(trades_dict=trades_dict, instrument_id=instId, algo_ids=[trade["sl_info"]["ordId"]])
                        await okx_trade.place_stop_loss_order(trades_dict=trades_dict, instrument_id=instId, trade_info=effective_trade_info, instrument_info=instrument_info)
                        await okx_trade.stop_breakeven(trades_dict=trades_dict, instrument_id=instId, trade_info=effective_trade_info, instrument_info=instrument_info)
                        pprint(trade, indent=2, width=80, sort_dicts=False)
                        await send_message(f"✅ TP1 {instId} — filled")

                elif trade["tp_info"][1]["ordId"] == order_id:

                    if state == "canceled":
                        logging.info(f"✅ {instId} TP2 {state} order {order_id}")
                        del trade["tp_info"]

                    elif state == "partially_filled":
                        logging.info(f"✅ {instId} TP2 {state} order {order_id} fill size: {item.get('accFillSz')}")
                        trade["current_fill_size"] -= float(item.get("accFillSz"))
                        trade["tp_info"][1]["price_filled"] = item.get("fillPx")
                        trade["tp_info"][1]["size_filled"] = item.get("accFillSz")
                        trade["tp_info"][1]["status"] = {state}

                     

                    elif state == "filled":
                        logging.info(f"✅ {instId} TP2 {state} order {order_id} fill size: {item.get('accFillSz')}")
                        trade["current_fill_size"] -= float(item.get("accFillSz"))
                        trade["tp_info"][1]["price_filled"] = item.get("fillPx")
                        trade["tp_info"][1]["size_filled"] = item.get("accFillSz")
                        trade["tp_info"][1]["status"] = {state}
                        if trade.get("sl_info", None) is not None:
                            await okx_trade.cancel_algo_orders(trades_dict=trades_dict, instrument_id=instId, algo_ids=[trade["sl_info"]["ordId"]])
                        await okx_trade.place_stop_loss_order(trades_dict=trades_dict, instrument_id=instId, trade_info=effective_trade_info, instrument_info=instrument_info)
                        await okx_trade.stop_breakeven(trades_dict=trades_dict, instrument_id=instId, trade_info=effective_trade_info, instrument_info=instrument_info)
                        pprint(trade, indent=2, width=80, sort_dicts=False)
                        await send_message(f"✅ TP2 {instId} — filled")

                elif trade["tp_info"][2]["ordId"] == order_id:
                    
                    if state == "canceled":
                        logging.info(f"✅ {instId} TP3 {state} order {order_id}")
                        del trade["tp_info"]

                    elif state == "partially_filled":
                        logging.info(f"✅ {instId} TP3 {state} order {order_id} fill size: {item.get('accFillSz')}")
                        trade["current_fill_size"] -= float(item.get("accFillSz"))
                        trade["tp_info"][2]["price_filled"] = item.get("fillPx")
                        trade["tp_info"][2]["size_filled"] = item.get("accFillSz")
                        trade["tp_info"][2]["status"] = {state}
                    

                    elif state == "filled":
                        logging.info(f"✅ {instId} TP3 {state} order {order_id} fill size: {item.get('accFillSz')}")
                        trade["current_fill_size"] -= float(item.get("accFillSz"))
                        trade["tp_info"][2]["price_filled"] = item.get("fillPx")
                        trade["tp_info"][2]["size_filled"] = item.get("accFillSz")
                        trade["tp_info"][2]["status"] = {state}
                        if trade.get("sl_info", None) is not None:
                            await okx_trade.cancel_algo_orders(trades_dict=trades_dict, instrument_id=instId, algo_ids=[trade["sl_info"]["ordId"]])
                        logging.info(f"✅✅✅ {instId} closes all position by TP")
                        trade["status"] = "closed"
                        await send_message(f"✅ TP {instId} — closed by TP")
                        pprint(trade, indent=2, width=80, sort_dicts=False)
                
                else:
                    logging.info(f'{instId} {order_id} not found in tp_info len: 3')

        if trade.get("sl_info", None) is not None:
            if trade["sl_info"]["ordId"] == order_id:

                if state == "canceled":
                    logging.info(f"✅ {instId} SL {state} order {order_id}")
                    del trade["sl_info"]

                elif state == "partially_filled":
                    logging.info(f"✅ {instId} SL {state} order {order_id} fill size: {item.get('accFillSz')}")
                    trade["current_fill_size"] -= float(item.get("accFillSz"))
                    trade["sl_info"]["price_filled"] = item.get("fillPx")
                    trade["sl_info"]["size_filled"] = item.get("accFillSz")
                    trade["sl_info"]["status"] = {state}

                elif state == "filled":
                    logging.info(f"✅ {instId} SL {state} order {order_id} fill size: {item.get('accFillSz')}")
                    trade["current_fill_size"] -= float(item.get("accFillSz"))
                    trade["sl_info"]["price_filled"] = item.get("fillPx")
                    trade["sl_info"]["size_filled"] = item.get("accFillSz")
                    trade["sl_info"]["status"] = {state}
                    logging.info(f"✅ {instId} SL {state} order {order_id} fill size: {item.get('accFillSz')}")
                    if trade.get("tp_info", None) is not None:
                        tp_ids = []
                        if len(trade["tp_info"]) == 1:
                            tp_ids.append(trade["tp_info"]["ordId"])
                        elif len(trade["tp_info"]) == 2:
                            tp_ids.append(trade["tp_info"][0]["ordId"])
                            tp_ids.append(trade["tp_info"][1]["ordId"])
                        elif len(trade["tp_info"]) == 3:
                            tp_ids.append(trade["tp_info"][0]["ordId"])
                            tp_ids.append(trade["tp_info"][1]["ordId"])
                            tp_ids.append(trade["tp_info"][2]["ordId"])
                        logging.info(f"tp_ids: {tp_ids}")
                        await okx_trade.cancel_orders(trades_dict=trades_dict, instrument_id=instId, ord_ids=tp_ids)
                    logging.info(f"✅✅✅ {instId} closes all position by SL")
                    trades_dict[effective_trade_info["message_id"]]["status"] = "closed"
                    await send_message(f"🛑 SL {instId} — closed by SL")
                    pprint(trade, indent=2, width=80, sort_dicts=False)














