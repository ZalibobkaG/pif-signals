import os
import asyncio
import time
from okx.api.public import Public
from okx.api.trade import Trade
from okx.api.algotrade import AlgoTrade
from okx.api.account import Account
from pprint import pprint

from loguru import logger
class OkxTrade:
    def __init__(self, api_key, api_secret, api_passphrase, balance: float = 100.0):
        self.api_key = api_key
        self.api_secret = api_secret
        self.api_passphrase = api_passphrase
        self.balance = balance
        self.publicData = Public(api_key, api_secret, api_passphrase)
        self.trade = Trade(api_key, api_secret, api_passphrase)
        self.algotrade = AlgoTrade(api_key, api_secret, api_passphrase)
        self.account = Account(api_key, api_secret, api_passphrase)

    def round_tick(self, value: float, tick_size: float = None) -> float:
        """
        Округляет value по количеству знаков после запятой, соответствующему tick_size.
        Если tick_size не задан, округляет до 2 знаков.
        """
        if tick_size is None:
            return round(value, 2)
        if tick_size >= 1:
            decimals = 0
        else:
            decimals = len(str(tick_size).split('.')[-1].rstrip('0'))
        return round(value, decimals)

    async def get_instrument_info(self, instrument_id: str, inst_type: str = "SWAP") -> tuple:
        res = await asyncio.to_thread(self.publicData.get_instruments, instType=inst_type, instId=instrument_id)
        if "data" not in res or not res["data"]:
            logger.warning(f"Не удалось получить данные инструмента {instrument_id}")
            return None, None, None, None
        info = res["data"][0]
        lot_size = float(info["lotSz"])
        ct_val = float(info["ctVal"])
        tick_size = float(info["tickSz"])
        min_size = float(info["minSz"])
        max_leverage = float(info["lever"])
        logger.info(f"{instrument_id} lot_size: {lot_size}, ct_val: {ct_val}, min_size: {min_size}, tick_size: {tick_size}, max_leverage: {max_leverage}")
        return lot_size, ct_val, min_size, tick_size, max_leverage

    async def set_leverage(self, instrument_id: str, leverage: int) -> bool:
        res = self.account.set_leverage(instId=instrument_id, lever=leverage, mgnMode="isolated")
        if res.get("code") != "0":
            logger.warning(f"{instrument_id} Failed to set leverage: {res.get('msg')}")
            return False
        return True

    async def place_new_order(self, trades_dict: dict, instrument_id: str, trade_info: dict, instrument_info: tuple) -> bool:
        lot_size, ct_val, min_size, tick_size, max_leverage = instrument_info
        side = "buy" if trade_info["side"] == "long" else "sell"

        if trade_info["entry"] == None:
            logger.info(f"{instrument_id} OKX entry is None - Using current price")
            entry = await self.get_curr_price(instrument_id)
            trade_info["entry"] = [entry]
        
        elif len(trade_info["entry"]) == 1:
            entry = trade_info["entry"][0]
            sl = trade_info["sl"]
            risk_percent = round(float(trade_info.get("risk", "1%").replace("%", "").strip()) * 0.95, 2)
            
            # Вычисляем расстояние до стоп-лосса в долларах (D)
            if trade_info["side"] == "long":
                sl_distance_usd = abs(entry - sl)
                sl_distance_percent = (sl_distance_usd / entry) * 100
            else:  # short
                sl_distance_usd = abs(sl - entry)
                sl_distance_percent = (sl_distance_usd / entry) * 100
            
            # Риск в долларах
            risk_usd = self.balance * risk_percent / 100
            
            position_nominal_usd = risk_usd / sl_distance_usd  # номинальная стоимость позиции в USDT
            position_size = position_nominal_usd / ct_val  # количество контрактов
            
            position_size = round(position_size / lot_size) * lot_size  # округление до lot_size
            position_size = max(position_size, min_size)

            position_nominal_usd = position_size * ct_val

            price_gap = sl_distance_percent * (1.3)
            print(f"price_gap: {price_gap}, sl_distance_percent: {sl_distance_percent}")
            max_leverage_by_liq = int(100/price_gap)


            # Ограничиваем плечо: не больше max_leverage биржи, не больше max_leverage_by_liq и не больше 20x по нашей стратегии
            safe_leverage = min(max_leverage, max_leverage_by_liq, 20)
            logger.info(f"{instrument_id} max_leverage_by_liq: {max_leverage_by_liq}")
            leverage = int(max(1, round(safe_leverage)))  # не меньше 1, округление
            
            
            sl_distance_percent = (sl_distance_usd / entry) * 100
            
            logger.info(f"{instrument_id} Position size: {position_size} contracts, Nominal: {position_nominal_usd:.2f} USD")
            
            # Устанавливаем плечо
            await self.set_leverage(instrument_id, leverage)

            # Выставляем ордер
            try:
                res = self.trade.set_order(
                    instId=instrument_id,
                    side=side,
                    ordType="limit",
                    tdMode="isolated",
                    sz=str(self.round_tick(position_size, lot_size)),
                    px=str(self.round_tick(entry, tick_size)),
                )

                # Сохраняем информацию о размещённом ордере в trades_dict
                if res.get("code") == "0" and res.get("data"):
                    ord_id = res["data"][0].get("ordId")
                    message_id = trade_info.get("message_id")
                    # Обновляем trades_dict: добавляем entry_ordId к существующей информации
                    if message_id not in trades_dict:
                        trades_dict[message_id] = {}
                    trades_dict[message_id].update(trade_info.copy())
                    trades_dict[message_id]["status"] = "active"
                    trades_dict[message_id]["entry_info"] = {"ordId": ord_id, "price_filled": None, "size": position_size, "size_filled": None, "status": "active"}
                    pprint(trades_dict[message_id], indent=2, width=80, sort_dicts=False)
                    logger.info(f"✅ {instrument_id} Updated trades_dict[{message_id}] with entry_ordId: {ord_id}")

            except Exception as e:
                logger.error(f"{instrument_id} Error placing order: {e}")
                return False
            return True

        elif len(trade_info["entry"]) >= 2:
            e0, e1 = trade_info["entry"][0], trade_info["entry"][1]
            sl = trade_info["sl"]
            # Первый ордер: у long — большая цена, у short — меньшая. Расстояние до SL — там же, с учётом long/short.
            if trade_info["side"] == "long":
                entry_first = max(e0, e1)
                entry_second = min(e0, e1)
                sl_distance_first = abs(entry_first - sl)
                sl_distance_second = abs(entry_second - sl)
            else:  # short
                entry_first = min(e0, e1)
                entry_second = max(e0, e1)
                sl_distance_first = abs(sl - entry_first)
                sl_distance_second = abs(sl - entry_second)

            risk_percent = round(float(trade_info.get("risk", "1%").replace("%", "").strip()) * 0.95, 2)
            risk_usd = self.balance * risk_percent / 100
            risk_first = risk_usd * 0.7
            risk_second = risk_usd * 0.3

            nominal_first = risk_first / sl_distance_first
            nominal_second = risk_second / sl_distance_second
            size_first = nominal_first / ct_val
            size_second = nominal_second / ct_val
            size_first = round(size_first / lot_size) * lot_size
            size_second = round(size_second / lot_size) * lot_size
            size_first = max(size_first, min_size)
            size_second = max(size_second, min_size)

            # Плечо — по максимальному расстоянию до SL (в % от цены входа)
            pct_first = (sl_distance_first / entry_first) * 100
            pct_second = (sl_distance_second / entry_second) * 100
            sl_distance_percent = max(pct_first, pct_second)
            price_gap = sl_distance_percent * (1 + 0.02)
            max_leverage_by_liq = int(100 / price_gap)
            # Не больше max_leverage биржи, не больше max_leverage_by_liq и не больше 20x
            leverage = int(max(1, min(max_leverage, max_leverage_by_liq, 20)))

            logger.info(f"{instrument_id} Two entries: first={entry_first}, second={entry_second}, size_first={size_first}, size_second={size_second}, leverage={leverage}x")
            await self.set_leverage(instrument_id, leverage)

            batch_orders = [
                {"instId": instrument_id, "tdMode": "isolated", "side": side, "ordType": "limit", "sz": str(self.round_tick(size_first, lot_size)), "px": str(self.round_tick(entry_first, tick_size))},
                {"instId": instrument_id, "tdMode": "isolated", "side": side, "ordType": "limit", "sz": str(self.round_tick(size_second, lot_size)), "px": str(self.round_tick(entry_second, tick_size))},
            ]
            ord_ids = []
            try:
                res = self.trade.set_batch_orders(batch_orders)
                logger.info(f"{instrument_id} Batch orders placed: {res}")
                if res.get("code") == "0" and res.get("data"):
                    for item in res["data"]:
                        if item.get("sCode") == "0" and item.get("ordId"):
                            ord_ids.append(item["ordId"])

                message_id = trade_info.get("message_id")
                if message_id and ord_ids:
                    if message_id not in trades_dict:
                        trades_dict[message_id] = {}
                    trades_dict[message_id].update(trade_info.copy())
                    trades_dict[message_id]["entry_ordId"] = ord_ids[0] if len(ord_ids) == 1 else ord_ids
                    trades_dict[message_id]["entry_info"] = [
                        {"ordId": ord_ids[0], "price_filled": None, "size": size_first, "size_filled": None, "status": "active"},
                        {"ordId": ord_ids[1], "price_filled": None, "size": size_second, "size_filled": None, "status": "active"}
                    ]
                    trades_dict[message_id]["status"] = "active"
                    pprint(trades_dict[message_id], indent=2, width=80, sort_dicts=False)
                    logger.info(f"✅ {instrument_id} Updated trades_dict[{message_id}] with entry_ordId(s): {ord_ids}")
            except Exception as e:
                logger.error(f"{instrument_id} Error placing two orders: {e}")
                return False
            return True

    async def place_tps_first_limit_order(self, trades_dict: dict, instrument_id: str, trade_info: dict, instrument_info: tuple) -> bool:
        lot_size, ct_val, min_size, tick_size, max_leverage = instrument_info
        side_to_close_position = "sell" if trade_info["side"] == "long" else "buy"
        trade = trades_dict[trade_info["message_id"]]
        total_size = float(trade["current_fill_size"])

        if len(trade["tp"]) == 1:
            try:
                res = self.trade.set_order(
                    instId=instrument_id,
                    side=side_to_close_position,
                    ordType="limit",
                    tdMode="isolated",
                    sz=str(total_size),
                    px=str(trade["tp"][0]),
                )
                if res.get("code") == "0" and res.get("data"):
                    tp_id = res["data"][0].get("ordId")
                    trade["tp_info"] = {"ordId": tp_id, "price_filled": None, "size": total_size, "size_filled": None, "status": "active"}
                    logger.info(f"✅ {instrument_id} Updated trades_dict {trade_info['message_id']} with tp_ordId: {tp_id}")
                    pprint(trade, indent=2, width=80, sort_dicts=False)
            except Exception as e:
                logger.error(f"{instrument_id} Error placing first tp limit order: {e}")
        
        elif len(trade["tp"]) == 2:
            tp_first_size = self.round_tick(total_size * 0.5, lot_size)
            tp_second_size = self.round_tick(total_size - tp_first_size, lot_size)
            
            batch_orders = [
                {"instId": instrument_id, "tdMode": "isolated", "side": side_to_close_position, "ordType": "limit", "sz": str(tp_first_size), "px": str(self.round_tick(trade["tp"][0], tick_size))},
                {"instId": instrument_id, "tdMode": "isolated", "side": side_to_close_position, "ordType": "limit", "sz": str(tp_second_size), "px": str(self.round_tick(trade["tp"][1], tick_size))},
            ]
            ord_ids = []
            try:
                res = self.trade.set_batch_orders(batch_orders)
                if res.get("code") == "0" and res.get("data"):
                    for item in res["data"]:
                        if item.get("sCode") == "0" and item.get("ordId"):
                            ord_ids.append(item["ordId"])
                    if ord_ids:
                        trade["tp_info"] = {"ordId": ord_ids[0], "price_filled": None, "size": tp_first_size, "size_filled": None, "status": "active"}, {"ordId": ord_ids[1], "price_filled": None, "size": tp_second_size, "size_filled": None, "status": "active"}
                        logger.info(f"✅ {instrument_id} Updated trades_dict {trade_info['message_id']} with tp_ordId(s): {ord_ids}")
                        pprint(trade, indent=2, width=80, sort_dicts=False)
            except Exception as e:
                logger.error(f"{instrument_id} Error placing second tp limit order: {e}")
                return False
            return True

        # Выставляем три тейк-профита
        else:
            tp_first_size = self.round_tick(total_size * 0.5, lot_size)
            tp_second_size = self.round_tick(total_size * 0.4, lot_size)
            tp_third_size = self.round_tick(total_size - tp_first_size - tp_second_size, lot_size)
            
            batch_orders = [
                {"instId": instrument_id, "tdMode": "isolated", "side": side_to_close_position, "ordType": "limit", "sz": str(tp_first_size), "px": str(self.round_tick(trade["tp"][0], tick_size))},
                {"instId": instrument_id, "tdMode": "isolated", "side": side_to_close_position, "ordType": "limit", "sz": str(tp_second_size), "px": str(self.round_tick(trade["tp"][1], tick_size))},
                {"instId": instrument_id, "tdMode": "isolated", "side": side_to_close_position, "ordType": "limit", "sz": str(tp_third_size), "px": str(self.round_tick(trade["tp"][2], tick_size))},
            ]
            ord_ids = []
            try:
                res = self.trade.set_batch_orders(batch_orders)
                if res.get("code") == "0" and res.get("data"):
                    for item in res["data"]:
                        if item.get("sCode") == "0" and item.get("ordId"):
                            ord_ids.append(item["ordId"])
                    if ord_ids:
                        trade["tp_info"] = {"ordId": ord_ids[0], "price_filled": None, "size": tp_first_size, "size_filled": None, "status": "active"}, {"ordId": ord_ids[1], "price_filled": None, "size": tp_second_size, "size_filled": None, "status": "active"}, {"ordId": ord_ids[2], "price_filled": None, "size": tp_third_size, "size_filled": None, "status": "active"}
                        logger.info(f"✅ {instrument_id} Updated trades_dict {trade_info['message_id']} with tp_ordId(s): {ord_ids}")
                        pprint(trade, indent=2, width=80, sort_dicts=False)
            except Exception as e:
                logger.error(f"{instrument_id} Error placing third tp limit order: {e}")
                return False
            return True

    async def place_tps_second_limit_order(self, trades_dict: dict, instrument_id: str, trade_info: dict, instrument_info: tuple) -> bool:
        lot_size, ct_val, min_size, tick_size, max_leverage = instrument_info
        side_to_close_position = "sell" if trade_info["side"] == "long" else "buy"
        trade = trades_dict[trade_info["message_id"]]
        total_size = float(trade["current_fill_size"])
        if len(trade["tp"]) == 1:
            try:
                res = self.trade.set_amend_order(
                    instId=instrument_id,
                    ordId=trade["tp_info"]["ordId"],
                    newSz=str(self.round_tick(total_size, lot_size)),
                )
                if res.get("code") == "0" and res.get("data"):
                    trade["tp_info"]["size"] = total_size
                    logger.info(f"✅ {instrument_id} Updated size of tp limit order")
                    pprint(trade, indent=2, width=80, sort_dicts=False)
            except Exception as e:
                logger.error(f"{instrument_id} Error placing first tp limit order: {e}")
        
        elif len(trade["tp"]) == 2:
            tp_first_size = self.round_tick(total_size * 0.5, lot_size)
            tp_second_size = self.round_tick(total_size - tp_first_size, lot_size)
            
            batch_orders = [
                {"instId": instrument_id, "ordId": trade["tp_info"][0]["ordId"], "newSz": str(tp_first_size)},
                {"instId": instrument_id, "ordId": trade["tp_info"][1]["ordId"], "newSz": str(tp_second_size)},
            ]
            ord_ids = []
            try:
                res = self.trade.set_amend_batch_orders(batch_orders)
                if res.get("code") == "0" and res.get("data"):
                    for item in res["data"]:
                        if item.get("sCode") == "0" and item.get("ordId"):
                            ord_ids.append(item["ordId"])
                    if ord_ids:
                        trade["tp_info"][0]["size"] = tp_first_size
                        trade["tp_info"][1]["size"] = tp_second_size
                        logger.info(f"✅ {instrument_id} Updated sizes of tp limits orders")
                        pprint(trade, indent=2, width=80, sort_dicts=False)
            except Exception as e:
                logger.error(f"{instrument_id} Error placing second tp limit order: {e}")
                return False
            return True

        # Выставляем три тейк-профита
        else:
            tp_first_size = self.round_tick(total_size * 0.5, lot_size)
            tp_second_size = self.round_tick(total_size * 0.4, lot_size)
            tp_third_size = self.round_tick(total_size - tp_first_size - tp_second_size, lot_size)
            
            batch_orders = [
                {"instId": instrument_id, "ordId": trade["tp_info"][0]["ordId"], "newSz": str(tp_first_size)},
                {"instId": instrument_id, "ordId": trade["tp_info"][1]["ordId"], "newSz": str(tp_second_size)},
                {"instId": instrument_id, "ordId": trade["tp_info"][2]["ordId"], "newSz": str(tp_third_size)},
            ]
            ord_ids = []
            try:
                res = self.trade.set_amend_batch_orders(batch_orders)
                if res.get("code") == "0" and res.get("data"):
                    for item in res["data"]:
                        if item.get("sCode") == "0" and item.get("ordId"):
                            ord_ids.append(item["ordId"])
                    if ord_ids:
                        trade["tp_info"][0]["size"] = tp_first_size
                        trade["tp_info"][1]["size"] = tp_second_size
                        trade["tp_info"][2]["size"] = tp_third_size
                        logger.info(f"✅ {instrument_id} Updated sizes of tp limits orders")
                        pprint(trade, indent=2, width=80, sort_dicts=False)
            except Exception as e:
                logger.error(f"{instrument_id} Error placing third tp limit order: {e}")
                return False
            return True

    async def cancel_orders(self, trades_dict: dict, instrument_id: str, ord_ids: list) -> bool:
        batch_orders = [
            {"instId": instrument_id, "ordId": ord_id} for ord_id in ord_ids
        ]
        try:
            res = self.trade.set_cancel_batch_orders(batch_orders)
            if res.get("code") == "0" and res.get("data"):
                logger.info(f"✅ {instrument_id} Cancelled orders {ord_ids}")
                return True
            else:
                logger.error(f"{instrument_id} Error cancelling orders {ord_ids}: {res.get('msg')}")
                return False
        except Exception as e:
            logger.error(f"{instrument_id} Error cancelling orders {ord_ids}: {e}")
            return False

    async def cancel_algo_orders(self, trades_dict: dict, instrument_id: str, algo_ids: list) -> bool:
        body = [{"algoId": str(aid), "instId": instrument_id} for aid in algo_ids]
        try:
            res = self.algotrade.send_request("/api/v5/trade/cancel-algos", "POST", body, proxies={}, proxy_host=None)
            if res.get("code") == "0":
                logger.info(f"✅ Cancelled algo orders {algo_ids} {instrument_id}")
                return True
            logger.error(f"Error cancelling algo orders {algo_ids} {instrument_id}: {res.get('msg')}")
            return False
        except Exception as e:
            logger.error(f"Error cancelling algo orders {algo_ids} {instrument_id}: {e}")
            return False
    
    async def stop_breakeven(self, trades_dict: dict, instrument_id: str, trade_info: dict, instrument_info: tuple) -> bool:
        lot_size, ct_val, min_size, tick_size, max_leverage = instrument_info
        side_to_close_position = "sell" if trade_info["side"] == "long" else "buy"
        trade = trades_dict[trade_info["message_id"]]
        current_fill_size = float(trade["current_fill_size"])
        sl_id = trade["sl_info"]["ordId"]
        if len(trade["entry"]) == 1:
            entry_price = trade["entry_info"]["price_filled"] if len(trade["entry"]) == 1 else trade["entry_info"][0]["price_filled"]
            breakeven_price = self.round_tick(entry_price * 1.005, tick_size) if side_to_close_position == "sell" else self.round_tick(entry_price * 0.995, tick_size)
        try:
            res = self.algotrade.set_amend_order(
                instId=instrument_id,
                algoId=sl_id,
                slTriggerPx=str(self.round_tick(breakeven_price * 1.002, tick_size) if side_to_close_position == "sell" else self.round_tick(breakeven_price * 0.998, tick_size)),
                slOrdPx=str(breakeven_price),
            )
            if res.get("code") == "0" and res.get("data"):
                trade["sl_info"]["breakeven"] = True
                logger.info(f"✅ {instrument_id} stop breakeven order updated")
                pprint(trade, indent=2, width=80, sort_dicts=False)
                return True
        except Exception as e:
            logger.error(f"{instrument_id} Error placing sl limit order: {e}")
            return False

    async def place_stop_loss_order(self, trades_dict: dict, instrument_id: str, trade_info: dict, instrument_info: tuple) -> bool:
        lot_size, ct_val, min_size, tick_size, max_leverage = instrument_info
        side_to_close_position = "sell" if trade_info["side"] == "long" else "buy"
        trade = trades_dict[trade_info["message_id"]]
        current_fill_size = float(trade["current_fill_size"])
        try:
            order = {
                "instId": instrument_id,
                "side": side_to_close_position,
                "ordType": "conditional",
                "posSide": "net",
                "tdMode": "isolated",
                "sz": str(current_fill_size),
                "reduceOnly": True,
                "slTriggerPx": str(self.round_tick(trade["sl"] * 1.002, tick_size) if side_to_close_position == "sell" else self.round_tick(trade["sl"] * 0.998, tick_size)),
                "slOrdPx": str(self.round_tick(trade["sl"], tick_size)),
            }
            res = self.algotrade.set_order_algo(**order)
            if res.get("code") == "0" and res.get("data"):
                trade["sl_info"] = {"ordId": res["data"][0].get("algoId"), "price_filled": None, "size": current_fill_size, "size_filled": None, "status": "active"}
                logger.info(f"✅ {instrument_id} stop loss order placed")
                pprint(trade, indent=2, width=80, sort_dicts=False)
            else:
                logger.warning(f"{instrument_id} place_stop_loss_order failed, full response: {res}")
        except Exception as e:
            logger.error(f"{instrument_id} Error placing stop loss order: {e}")
            return False
        return True

    async def close_entire_position(self, trades_dict: dict, instrument_id: str, trade_info: dict, instrument_info: tuple) -> bool:
        lot_size, ct_val, min_size, tick_size, max_leverage = instrument_info
        side_to_close_position = "sell" if trade_info["side"] == "long" else "buy"
        trade = trades_dict[trade_info["message_id"]]
        current_fill_size = float(trade["current_fill_size"])
        try:
            res = self.trade.set_order(
                instId=instrument_id,
                ordType="market",
                side=side_to_close_position,
                tdMode="isolated",
                sz=str(self.round_tick(current_fill_size, lot_size)),
            )
            if res.get("code") == "0" and res.get("data"):
                trade["status"] = "closed"
                ord_id = res["data"][0].get("ordId")
                filled_price = float(res["data"][0].get("fillPx", 0))

                logger.info(f"✅ {instrument_id} closed entire position {ord_id} at {filled_price}")
                pprint(trade, indent=2, width=80, sort_dicts=False)
                return True
        except Exception as e:
            logger.error(f"{instrument_id} Error closing entire position: {e}")
            return False
        return True

    async def get_curr_price(self, instrument_id: str) -> float:
        res = self.publicData.get_index_tickers(instId=instrument_id.replace("-SWAP", ""))
        if res.get("code") == "0" and res.get("data"):
            return float(res["data"][0].get("idxPx", 0))
        return 0

    async def enter_now(self, trades_dict: dict, instrument_id: str, trade_info: dict, instrument_info: tuple) -> bool:
        lot_size, ct_val, min_size, tick_size, max_leverage = instrument_info
        trade = trades_dict[trade_info["message_id"]]
        okx_side = "buy" if trade["side"] == "long" else "sell"
        curr_price = await self.get_curr_price(instrument_id)
        print(f"curr_price: {curr_price}")
        if curr_price != 0:
            entry = curr_price
        sl = trade["sl"]
        risk_percent = round(float(trade.get("risk", "1%").replace("%", "").strip()) * 0.95, 2)
        
        # Вычисляем расстояние до стоп-лосса в долларах (D)
        if trade["side"] == "long":
            sl_distance_usd = abs(entry - sl)
            sl_distance_percent = (sl_distance_usd / entry) * 100
        else:  # short
            sl_distance_usd = abs(sl - entry)
            sl_distance_percent = (sl_distance_usd / entry) * 100
        
        # Риск в долларах
        risk_usd = self.balance * risk_percent / 100
        
        position_nominal_usd = risk_usd / sl_distance_usd  # номинальная стоимость позиции в USDT
        position_size = position_nominal_usd / ct_val  # количество контрактов
        
        position_size = round(position_size / lot_size) * lot_size  # округление до lot_size
        position_size = max(position_size, min_size)

        position_nominal_usd = position_size * ct_val

        price_gap = sl_distance_percent * 1.3
        print(f"price_gap: {price_gap}, sl_distance_percent: {sl_distance_percent}")
        max_leverage_by_liq = int(100/price_gap)


        # Ограничиваем плечо: не больше max_leverage биржи, не больше max_leverage_by_liq и не больше 20x
        safe_leverage = min(max_leverage, max_leverage_by_liq, 20)
        logger.info(f"{instrument_id} max_leverage_by_liq: {max_leverage_by_liq}")
        leverage = int(max(1, round(safe_leverage)))  # не меньше 1, округление
        
        
        sl_distance_percent = (sl_distance_usd / entry) * 100
        
        logger.info(f"{instrument_id} Position size: {position_size} contracts, Nominal: {position_nominal_usd:.2f} USD")
        
        # Устанавливаем плечо
        await self.set_leverage(instrument_id, leverage)

        # Выставляем ордер
        try:
            res = self.trade.set_order(
                instId=instrument_id,
                side=okx_side,
                ordType="market",
                tdMode="isolated",
                sz=str(self.round_tick(position_size, lot_size)),
            )

            # Сохраняем информацию о размещённом ордере в trades_dict
            if res.get("code") == "0" and res.get("data"):
                logger.info(f"✅ {instrument_id} entered now at {curr_price}")
                ord_id = res["data"][0].get("ordId")
                message_id = trade.get("message_id")
                # Обновляем trades_dict: добавляем entry_ordId к существующей информации
                if message_id not in trades_dict:
                    trades_dict[message_id] = {}
                trades_dict[message_id].update(trade.copy())
                trades_dict[message_id]["status"] = "active"
                pprint(trades_dict[message_id], indent=2, width=80, sort_dicts=False)
                if len(trade["entry"]) == 2:
                    trades_dict[message_id]["entry"].pop(-1)
                trades_dict[message_id]["entry_info"] = {"ordId": ord_id, "price_filled": None, "size": position_size, "size_filled": None, "status": "active"}
                logger.info(f"✅ {instrument_id} Updated trades_dict[{message_id}] with entry_ordId: {ord_id}")
                pprint(trades_dict[message_id], indent=2, width=80, sort_dicts=False)
            else:
                logger.info(f"{instrument_id} Error placing order: {res.get('msg')}")
                return False

        except Exception as e:
            logger.info(f"{instrument_id} Error placing order: {e}")
            return False
        return True




if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    api_key = os.environ.get("OKX_API_KEY", "")
    api_secret = os.environ.get("OKX_SECRET_KEY", "")  # в .env называется OKX_SECRET_KEY
    passphrase = os.environ.get("OKX_PASSPHRASE", "")
    okx = OkxTrade(api_key=api_key, api_secret=api_secret, api_passphrase=passphrase)
    coin_okx = "WLFI-USDT-SWAP"
    trade_info = {"action": "place_new_order",
      "trader": "trader #A",
      "coin": "WLFIUSDT",
      "side": "long",
      "entry": [0.096],
      "sl": 0.09,
      "tp": [0.12, 0.124, 0.129],
      "risk": "1%",
      "exchange": "OKX",
      "message_id": 2066,
      }
    coin_info = asyncio.run(okx.get_instrument_info(instrument_id=coin_okx))
    trades_dict = {}
    place_result = asyncio.run(okx.place_new_order(trades_dict=trades_dict, instrument_id=coin_okx, trade_info=trade_info, instrument_info=coin_info))
    pprint(trades_dict, indent=2, width=80, sort_dicts=False)
    time.sleep(1)
    enter_now_result = asyncio.run(okx.enter_now(trades_dict=trades_dict, instrument_id=coin_okx, trade_info=trade_info, instrument_info=coin_info))
    pprint(trades_dict, indent=2, width=80, sort_dicts=False)
    trades_dict[trade_info["message_id"]]["current_fill_size"] = 6
    time.sleep(1)
    sl_result = asyncio.run(okx.place_stop_loss_order(trades_dict=trades_dict, instrument_id=coin_okx, trade_info=trade_info, instrument_info=coin_info))

