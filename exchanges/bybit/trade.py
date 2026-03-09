import logging
import os
import asyncio
import math
from pybit.unified_trading import HTTP
from pprint import pprint
import time

logging.basicConfig(level=logging.INFO)
_logs_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "logs"))
os.makedirs(_logs_dir, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(_logs_dir, "logarik.log"), encoding="utf-8"),
        logging.StreamHandler()
    ]
)
class BybitTrade:
    def __init__(self, api_key, api_secret, balance: float = 100.0):
        self.api_key = api_key
        self.api_secret = api_secret
        self.balance = balance
        self.market_session = HTTP(testnet=False)
        self.trade_session = HTTP(testnet=False, api_key=api_key, api_secret=api_secret)

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

    async def get_instrument_info(self, symbol: str, category: str = "linear") -> tuple:
        """
        Получает информацию об инструменте на Bybit.
        Возвращает: (max_leverage, tick_size, min_order_qty, qty_step, min_notional_value)
        или (None, None, None, None, None) при ошибке.
        
        Args:
            symbol: Символ инструмента (например "BTCUSDT")
            category: Тип продукта ("linear" для USDT perpetual, "spot", "inverse", "option")
        """
        try:
            # Вызываем синхронный метод через asyncio.to_thread
            res = await asyncio.to_thread(
                self.market_session.get_instruments_info,
                category=category,
                symbol=symbol
            )
            
            if res.get("retCode") != 0:
                logging.warning(f"Bybit API error: {res.get('retMsg')}")
                return None, None, None, None, None
            
            result = res.get("result", {})
            instruments_list = result.get("list", [])
            
            if not instruments_list:
                logging.warning(f"Instrument {symbol} not found on Bybit")
                return None, None, None, None, None
            
            # Берём первый инструмент из списка
            instrument = instruments_list[0]
            
            # Извлекаем нужные поля
            leverage_filter = instrument.get("leverageFilter", {})
            max_leverage = float(leverage_filter.get("maxLeverage", "1")) if leverage_filter else 1.0
            
            price_filter = instrument.get("priceFilter", {})
            tick_size = float(price_filter.get("tickSize", "0.01")) if price_filter else 0.01
            
            lot_size_filter = instrument.get("lotSizeFilter", {})
            min_order_qty = float(lot_size_filter.get("minOrderQty", "0.001")) if lot_size_filter else 0.001
            qty_step = float(lot_size_filter.get("qtyStep", "0.001")) if lot_size_filter else 0.001
            # Минимальный notional в квоте (USDT) — minNotionalValue для linear
            min_notional_value = float(lot_size_filter.get("minNotionalValue", "0")) if lot_size_filter else 0.0
            
            logging.info(
                "Bybit %s: max_leverage=%s, tick_size=%s, min_order_qty=%s, qty_step=%s, min_notional_value=%s",
                symbol,
                max_leverage,
                tick_size,
                min_order_qty,
                qty_step,
                min_notional_value,
            )
            return max_leverage, tick_size, min_order_qty, qty_step, min_notional_value
            
        except Exception as e:
            logging.exception(f"Error getting Bybit instrument info for {symbol}: {e}")
            return None, None, None, None, None

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        """
        Устанавливает плечо на Bybit (v5 /position/set-leverage).
        Код 110043 "leverage not modified" считаем успешным результатом.
        """
        try:
            res = await asyncio.to_thread(
                self.trade_session.set_leverage,
                category="linear",
                symbol=symbol,
                buyLeverage=str(leverage),
                sellLeverage=str(leverage),
            )
        except Exception as e:
            msg = str(e)
            if "110043" in msg or "leverage not modified" in msg:
                logging.info("%s Bybit set_leverage: already %sx", symbol, leverage)
                return True
            logging.info("❗️ Bybit set_leverage exception: %s", e)
            return False

        code = res.get("retCode")
        if code == 0:
            return True

        logging.info("❗️ Bybit API error (set_leverage): %s (%s)", res.get("retMsg"), code)
        return False

    async def place_new_order(self, trades_dict: dict, instrument_id: str, trade_info: dict, instrument_info: tuple) -> bool:
        """
        Аналог OKX.place_new_order, но под Bybit linear (USDT perpetual).
        instrument_info: (max_leverage, tick_size, min_order_qty, qty_step, min_notional_value)
        """
        max_leverage, tick_size, min_order_qty, qty_step, min_notional_value = instrument_info
        side = "Buy" if trade_info["side"] == "long" else "Sell"

        if len(trade_info["entry"]) == 1:
            entry = trade_info["entry"][0]
            sl = trade_info["sl"]
            risk_percent = round(float(trade_info.get("risk", "1%").replace("%", "").strip()) * 0.95, 2)

            if trade_info["side"] == "long":
                sl_distance_usd = abs(entry - sl)
                sl_distance_percent = (sl_distance_usd / entry) * 100
            else:
                sl_distance_usd = abs(sl - entry)
                sl_distance_percent = (sl_distance_usd / entry) * 100

            risk_usd = self.balance * risk_percent / 100

            # Bybit linear: qty в базе, loss = qty * sl_distance_usd → qty = risk_usd / sl_distance_usd
            qty = risk_usd / sl_distance_usd
            qty = round(qty / qty_step) * qty_step
            qty = max(qty, min_order_qty)

            position_nominal_usd = qty * entry
            if min_notional_value and position_nominal_usd < min_notional_value:
                required_qty = min_notional_value / entry
                required_qty = math.ceil(required_qty / qty_step) * qty_step
                required_qty = max(required_qty, min_order_qty)
                logging.info(
                    "%s Bybit place_new_order: nominal %.4f < min_notional_value %.4f, bump qty %.4f → %.4f",
                    instrument_id,
                    position_nominal_usd,
                    min_notional_value,
                    qty,
                    required_qty,
                )
                qty = required_qty
                position_nominal_usd = qty * entry

            price_gap = sl_distance_percent * 1.1
            max_leverage_by_liq = int(100 / price_gap)
            safe_leverage = min(max_leverage, max_leverage_by_liq, 20)
            leverage = int(max(1, round(safe_leverage)))

            logging.info(f"{instrument_id} Bybit Position size: {qty}, Nominal: {position_nominal_usd:.2f} USD")
            await self.set_leverage(instrument_id, leverage)

            try:
                res = await asyncio.to_thread(
                    self.trade_session.place_order,
                    category="linear",
                    symbol=instrument_id,
                    side=side,
                    orderType="Limit",
                    qty=str(self.round_tick(qty, qty_step)),
                    price=str(self.round_tick(entry, tick_size)),
                    reduceOnly=False,
                )

                if res.get("retCode") == 0 and res.get("result"):
                    ord_id = res["result"].get("orderId")
                    message_id = trade_info.get("message_id")
                    if message_id not in trades_dict:
                        trades_dict[message_id] = {}
                    trades_dict[message_id].update(trade_info.copy())
                    trades_dict[message_id]["status"] = "active"
                    trades_dict[message_id]["entry_info"] = {"ordId": ord_id, "price_filled": None, "size": qty, "size_filled": None, "status": "active"}
                    pprint(trades_dict[message_id], indent=2, width=80, sort_dicts=False)
                    logging.info(f"✅ {instrument_id} Updated trades_dict[{message_id}] with entry_ordId: {ord_id}")

            except Exception as e:
                logging.error(f"{instrument_id} Error placing order: {e}")
                return False
            return True

        elif len(trade_info["entry"]) == 2:
            e0, e1 = trade_info["entry"][0], trade_info["entry"][1]
            sl = trade_info["sl"]
            if trade_info["side"] == "long":
                entry_first = max(e0, e1)
                entry_second = min(e0, e1)
                sl_distance_first = abs(entry_first - sl)
                sl_distance_second = abs(entry_second - sl)
            else:
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
            qty_first = nominal_first
            qty_second = nominal_second
            qty_first = round(qty_first / qty_step) * qty_step
            qty_second = round(qty_second / qty_step) * qty_step
            qty_first = max(qty_first, min_order_qty)
            qty_second = max(qty_second, min_order_qty)

            # Проверка минимального notional для каждого входа отдельно
            if min_notional_value:
                notional_first = qty_first * entry_first
                if notional_first < min_notional_value:
                    required_qty_first = min_notional_value / entry_first
                    required_qty_first = math.ceil(required_qty_first / qty_step) * qty_step
                    required_qty_first = max(required_qty_first, min_order_qty)
                    logging.info(
                        "%s Bybit place_new_order (2 entries): notional_first %.4f < min_notional_value %.4f, "
                        "bump qty_first %.4f → %.4f",
                        instrument_id,
                        notional_first,
                        min_notional_value,
                        qty_first,
                        required_qty_first,
                    )
                    qty_first = required_qty_first

                notional_second = qty_second * entry_second
                if notional_second < min_notional_value:
                    required_qty_second = min_notional_value / entry_second
                    required_qty_second = math.ceil(required_qty_second / qty_step) * qty_step
                    required_qty_second = max(required_qty_second, min_order_qty)
                    logging.info(
                        "%s Bybit place_new_order (2 entries): notional_second %.4f < min_notional_value %.4f, "
                        "bump qty_second %.4f → %.4f",
                        instrument_id,
                        notional_second,
                        min_notional_value,
                        qty_second,
                        required_qty_second,
                    )
                    qty_second = required_qty_second

            pct_first = (sl_distance_first / entry_first) * 100
            pct_second = (sl_distance_second / entry_second) * 100
            sl_distance_percent = max(pct_first, pct_second)
            price_gap = sl_distance_percent * (1 + 0.02)
            max_leverage_by_liq = int(100 / price_gap)
            leverage = int(max(1, min(max_leverage, max_leverage_by_liq, 20)))

            logging.info(f"{instrument_id} Bybit Two entries: first={entry_first}, second={entry_second}, qty_first={qty_first}, qty_second={qty_second}, leverage={leverage}x")
            await self.set_leverage(instrument_id, leverage)

            request = [
                {"symbol": instrument_id, "side": side, "orderType": "Limit", "qty": str(self.round_tick(qty_first, qty_step)), "price": str(self.round_tick(entry_first, tick_size)), "reduceOnly": False},
                {"symbol": instrument_id, "side": side, "orderType": "Limit", "qty": str(self.round_tick(qty_second, qty_step)), "price": str(self.round_tick(entry_second, tick_size)), "reduceOnly": False},
            ]
            ord_ids = []
            try:
                res = await asyncio.to_thread(
                    self.trade_session.place_batch_order,
                    category="linear",
                    request=request,
                )
                logging.info(f"{instrument_id} Bybit Batch orders placed: {res}")
                if res.get("retCode") == 0 and res.get("result"):
                    for item in res["result"].get("list", []):
                        if item.get("orderId"):
                            ord_ids.append(item["orderId"])

                message_id = trade_info.get("message_id")
                if message_id and ord_ids:
                    if message_id not in trades_dict:
                        trades_dict[message_id] = {}
                    trades_dict[message_id].update(trade_info.copy())
                    trades_dict[message_id]["entry_ordId"] = ord_ids[0] if len(ord_ids) == 1 else ord_ids
                    trades_dict[message_id]["entry_info"] = [
                        {"ordId": ord_ids[0], "price_filled": None, "size": qty_first, "size_filled": None, "status": "active"},
                        {"ordId": ord_ids[1], "price_filled": None, "size": qty_second, "size_filled": None, "status": "active"}
                    ]
                    trades_dict[message_id]["status"] = "active"
                    pprint(trades_dict[message_id], indent=2, width=80, sort_dicts=False)
                    logging.info(f"✅ {instrument_id} Updated trades_dict[{message_id}] with entry_ordId(s): {ord_ids}")
            except Exception as e:
                logging.error(f"{instrument_id} Error placing two orders: {e}")
                return False
            return True

    async def place_tps_first_limit_order(self, trades_dict: dict, instrument_id: str, trade_info: dict, instrument_info: tuple) -> bool:
        """Выставить TP лимитные ордера после первого fill. Структура как у OKX."""
        max_leverage, tick_size, min_order_qty, qty_step, min_notional_value = instrument_info
        side_to_close_position = "Sell" if trade_info["side"] == "long" else "Buy"
        trade = trades_dict[trade_info["message_id"]]
        total_size = float(trade["current_fill_size"])

        if len(trade["tp"]) == 1:
            try:
                res = await asyncio.to_thread(
                    self.trade_session.place_order,
                    category="linear",
                    symbol=instrument_id,
                    side=side_to_close_position,
                    orderType="Limit",
                    qty=str(self.round_tick(total_size, qty_step)),
                    price=str(self.round_tick(trade["tp"][0], tick_size)),
                    reduceOnly=True,
                )
                if res.get("retCode") == 0 and res.get("result"):
                    tp_id = res["result"].get("orderId")
                    trade["tp_info"] = {"ordId": tp_id, "price_filled": None, "size": total_size, "size_filled": None, "status": "active"}
                    logging.info(f"✅ {instrument_id} Bybit Updated trades_dict {trade_info['message_id']} with tp_ordId: {tp_id}")
            except Exception as e:
                logging.error(f"{instrument_id} Bybit Error placing tp limit order: {e}")
                return False
            return True

        elif len(trade["tp"]) == 2:
            tp_first_size = self.round_tick(total_size * 0.5, qty_step)
            tp_second_size = self.round_tick(total_size - tp_first_size, qty_step)

            request = [
                {"symbol": instrument_id, "side": side_to_close_position, "orderType": "Limit", "qty": str(tp_first_size), "price": str(self.round_tick(trade["tp"][0], tick_size)), "reduceOnly": True},
                {"symbol": instrument_id, "side": side_to_close_position, "orderType": "Limit", "qty": str(tp_second_size), "price": str(self.round_tick(trade["tp"][1], tick_size)), "reduceOnly": True},
            ]
            ord_ids = []
            try:
                res = await asyncio.to_thread(
                    self.trade_session.place_batch_order,
                    category="linear",
                    request=request,
                )
                if res.get("retCode") == 0 and res.get("result"):
                    for item in res["result"].get("list", []):
                        if item.get("orderId"):
                            ord_ids.append(item["orderId"])
                if ord_ids:
                    trade["tp_info"] = [
                        {"ordId": ord_ids[0], "price_filled": None, "size": tp_first_size, "size_filled": None, "status": "active"},
                        {"ordId": ord_ids[1], "price_filled": None, "size": tp_second_size, "size_filled": None, "status": "active"},
                    ]
                    logging.info(f"✅ {instrument_id} Bybit Updated trades_dict {trade_info['message_id']} with tp_ordId(s): {ord_ids}")
            except Exception as e:
                logging.error(f"{instrument_id} Bybit Error placing tp batch orders: {e}")
                return False
            return True

        else:
            tp_first_size = self.round_tick(total_size * 0.5, qty_step)
            tp_second_size = self.round_tick(total_size * 0.4, qty_step)
            tp_third_size = self.round_tick(total_size - tp_first_size - tp_second_size, qty_step)

            request = [
                {"symbol": instrument_id, "side": side_to_close_position, "orderType": "Limit", "qty": str(tp_first_size), "price": str(self.round_tick(trade["tp"][0], tick_size)), "reduceOnly": True},
                {"symbol": instrument_id, "side": side_to_close_position, "orderType": "Limit", "qty": str(tp_second_size), "price": str(self.round_tick(trade["tp"][1], tick_size)), "reduceOnly": True},
                {"symbol": instrument_id, "side": side_to_close_position, "orderType": "Limit", "qty": str(tp_third_size), "price": str(self.round_tick(trade["tp"][2], tick_size)), "reduceOnly": True},
            ]
            ord_ids = []
            try:
                res = await asyncio.to_thread(
                    self.trade_session.place_batch_order,
                    category="linear",
                    request=request,
                )
                if res.get("retCode") == 0 and res.get("result"):
                    for item in res["result"].get("list", []):
                        if item.get("orderId"):
                            ord_ids.append(item["orderId"])
                if ord_ids:
                    trade["tp_info"] = [
                        {"ordId": ord_ids[0], "price_filled": None, "size": tp_first_size, "size_filled": None, "status": "active"},
                        {"ordId": ord_ids[1], "price_filled": None, "size": tp_second_size, "size_filled": None, "status": "active"},
                        {"ordId": ord_ids[2], "price_filled": None, "size": tp_third_size, "size_filled": None, "status": "active"},
                    ]
                    logging.info(f"✅ {instrument_id} Bybit Updated trades_dict {trade_info['message_id']} with tp_ordId(s): {ord_ids}")
            except Exception as e:
                logging.error(f"{instrument_id} Bybit Error placing tp batch orders (3): {e}")
                return False
            return True

    async def place_tps_second_limit_order(self, trades_dict: dict, instrument_id: str, trade_info: dict, instrument_info: tuple) -> bool:
        """Обновить размер TP ордеров после второго fill. Структура как у OKX."""
        max_leverage, tick_size, min_order_qty, qty_step, min_notional_value = instrument_info
        trade = trades_dict[trade_info["message_id"]]
        total_size = float(trade["current_fill_size"])

        if len(trade["tp"]) == 1:
            try:
                res = await asyncio.to_thread(
                    self.trade_session.amend_order,
                    category="linear",
                    symbol=instrument_id,
                    orderId=trade["tp_info"]["ordId"],
                    qty=str(self.round_tick(total_size, qty_step)),
                )
                if res.get("retCode") == 0:
                    trade["tp_info"]["size"] = total_size
                    logging.info(f"✅ {instrument_id} Bybit Updated size of tp limit order")
            except Exception as e:
                logging.error(f"{instrument_id} Bybit Error amending tp order: {e}")
                return False
            return True

        elif len(trade["tp"]) == 2:
            tp_first_size = self.round_tick(total_size * 0.5, qty_step)
            tp_second_size = self.round_tick(total_size - tp_first_size, qty_step)

            request = [
                {"symbol": instrument_id, "orderId": trade["tp_info"][0]["ordId"], "qty": str(tp_first_size)},
                {"symbol": instrument_id, "orderId": trade["tp_info"][1]["ordId"], "qty": str(tp_second_size)},
            ]
            try:
                res = await asyncio.to_thread(
                    self.trade_session.amend_batch_order,
                    category="linear",
                    request=request,
                )
                if res.get("retCode") == 0:
                    trade["tp_info"][0]["size"] = tp_first_size
                    trade["tp_info"][1]["size"] = tp_second_size
                    logging.info(f"✅ {instrument_id} Bybit Updated sizes of tp limit orders")
            except Exception as e:
                logging.error(f"{instrument_id} Bybit Error amending tp batch orders: {e}")
                return False
            return True

        else:
            tp_first_size = self.round_tick(total_size * 0.5, qty_step)
            tp_second_size = self.round_tick(total_size * 0.4, qty_step)
            tp_third_size = self.round_tick(total_size - tp_first_size - tp_second_size, qty_step)

            request = [
                {"symbol": instrument_id, "orderId": trade["tp_info"][0]["ordId"], "qty": str(tp_first_size)},
                {"symbol": instrument_id, "orderId": trade["tp_info"][1]["ordId"], "qty": str(tp_second_size)},
                {"symbol": instrument_id, "orderId": trade["tp_info"][2]["ordId"], "qty": str(tp_third_size)},
            ]
            try:
                res = await asyncio.to_thread(
                    self.trade_session.amend_batch_order,
                    category="linear",
                    request=request,
                )
                if res.get("retCode") == 0:
                    trade["tp_info"][0]["size"] = tp_first_size
                    trade["tp_info"][1]["size"] = tp_second_size
                    trade["tp_info"][2]["size"] = tp_third_size
                    logging.info(f"✅ {instrument_id} Bybit Updated sizes of tp limit orders (3)")
            except Exception as e:
                logging.error(f"{instrument_id} Bybit Error amending tp batch orders (3): {e}")
                return False
            return True

    async def get_curr_price(self, symbol: str, category: str = "linear") -> float:
        """
        Получает текущую цену через /v5/market/tickers.
        Возвращает lastPrice или 0 при ошибке.
        """
        try:
            res = await asyncio.to_thread(
                self.market_session.get_tickers,
                category=category,
                symbol=symbol.upper(),
            )
            if res.get("retCode") == 0 and res.get("result", {}).get("list"):
                last_price = res["result"]["list"][0].get("lastPrice")
                return float(last_price) if last_price else 0.0
            return 0.0
        except Exception as e:
            logging.exception(f"Bybit get_curr_price {symbol}: {e}")
            return 0.0
    
    async def enter_now(self, trades_dict: dict, instrument_id: str, trade_info: dict, instrument_info: tuple) -> bool:
        """Вход по рынку. См. OKX.enter_now и https://bybit-exchange.github.io/docs/v5/order/create-order"""
        max_leverage, tick_size, min_order_qty, qty_step, min_notional_value = instrument_info
        trade = trades_dict[trade_info["message_id"]]
        side = "Buy" if trade["side"] == "long" else "Sell"

        curr_price = await self.get_curr_price(instrument_id)
        if curr_price == 0:
            logging.warning(f"{instrument_id} Bybit enter_now: curr_price=0")
            return False

        entry = curr_price
        sl = float(trade["sl"])
        risk_percent = round(float(trade.get("risk", "1%").replace("%", "").strip()) * 0.95, 2)

        if trade["side"] == "long":
            sl_distance_usd = abs(entry - sl)
            sl_distance_percent = (sl_distance_usd / entry) * 100
        else:
            sl_distance_usd = abs(sl - entry)
            sl_distance_percent = (sl_distance_usd / entry) * 100

        risk_usd = self.balance * risk_percent / 100
        qty = risk_usd / sl_distance_usd
        qty = round(qty / qty_step) * qty_step
        qty = max(qty, min_order_qty)

        position_nominal_usd = qty * entry
        if min_notional_value and position_nominal_usd < min_notional_value:
            required_qty = min_notional_value / entry
            required_qty = math.ceil(required_qty / qty_step) * qty_step
            required_qty = max(required_qty, min_order_qty)
            logging.info(
                "%s Bybit enter_now: nominal %.4f < min_notional_value %.4f, bump qty %.4f → %.4f",
                instrument_id,
                position_nominal_usd,
                min_notional_value,
                qty,
                required_qty,
            )
            qty = required_qty
            position_nominal_usd = qty * entry

        price_gap = sl_distance_percent * (1 + 0.02)
        max_leverage_by_liq = int(100 / price_gap)
        safe_leverage = min(max_leverage, max_leverage_by_liq, 20)
        leverage = int(max(1, round(safe_leverage)))

        logging.info(f"{instrument_id} Bybit enter_now: qty={qty}, nominal={position_nominal_usd:.2f} USD, leverage={leverage}x")
        await self.set_leverage(instrument_id, leverage)

        try:
            res = await asyncio.to_thread(
                self.trade_session.place_order,
                category="linear",
                symbol=instrument_id,
                side=side,
                orderType="Market",
                qty=str(self.round_tick(qty, qty_step)),
                reduceOnly=False,
            )

            if res.get("retCode") == 0 and res.get("result"):
                ord_id = res["result"].get("orderId")
                message_id = trade.get("message_id")
                if message_id not in trades_dict:
                    trades_dict[message_id] = {}
                trades_dict[message_id].update(trade.copy())
                trades_dict[message_id]["status"] = "active"
                if len(trade["entry"]) == 2:
                    trades_dict[message_id]["entry"].pop(-1)
                trades_dict[message_id]["entry_info"] = {"ordId": ord_id, "price_filled": None, "size": qty, "size_filled": None, "status": "active"}
                logging.info(f"✅ {instrument_id} Bybit entered now at {curr_price}, entry_ordId: {ord_id}")
            else:
                logging.warning(f"{instrument_id} Bybit enter_now API error: {res.get('retMsg')}")
                return False
        except Exception as e:
            logging.error(f"{instrument_id} Bybit enter_now error: {e}")
            return False
        return True

    async def cancel_orders(self, trades_dict: dict, instrument_id: str, ord_ids: list) -> bool:
        """Отмена ордеров. См. OKX.cancel_orders и https://bybit-exchange.github.io/docs/v5/order/batch-cancel"""
        if not ord_ids:
            return True
        request = [{"symbol": instrument_id, "orderId": str(oid)} for oid in ord_ids]
        try:
            res = await asyncio.to_thread(
                self.trade_session.cancel_batch_order,
                category="linear",
                request=request,
            )
            print(res)
            if res.get("retCode") == 0:
                logging.info(f"✅ {instrument_id} Bybit Cancelled orders {ord_ids}")
                return True
            logging.error(f"{instrument_id} Bybit Error cancelling orders {ord_ids}: {res.get('retMsg')}")
            return False
        except Exception as e:
            logging.error(f"{instrument_id} Bybit Error cancelling orders {ord_ids}: {e}")
            return False

    async def place_stop_loss_order(self, trades_dict: dict, instrument_id: str, trade_info: dict, instrument_info: tuple) -> bool:
        """
        Выставить стоп-лосс через conditional order (place_order + triggerPrice).
        См. OKX.place_stop_loss_order и https://bybit-exchange.github.io/docs/v5/order/create-order
        """
        max_leverage, tick_size, min_order_qty, qty_step, min_notional_value = instrument_info
        trade = trades_dict[trade_info["message_id"]]
        current_fill_size = float(trade["current_fill_size"])
        sl_price = self.round_tick(float(trade["sl"]), tick_size)
        side = "Sell" if trade_info["side"] == "long" else "Buy"
        # triggerDirection: 2 = fall to trigger (long→sell), 1 = rise to trigger (short→buy)
        trigger_direction = 2 if trade_info["side"] == "long" else 1

        # Делаем стоп лимитным, чтобы контролировать проскальзывание:
        # - для лонга лимит чуть ниже триггера (sell stop)
        # - для шорта лимит чуть выше триггера (buy stop)
        if trade_info["side"] == "long":
            limit_price = self.round_tick(sl_price * 0.999, tick_size)
        else:
            limit_price = self.round_tick(sl_price * 1.001, tick_size)

        try:
            res = await asyncio.to_thread(
                self.trade_session.place_order,
                category="linear",
                symbol=instrument_id,
                side=side,
                orderType="Limit",
                qty=str(self.round_tick(current_fill_size, qty_step)),
                price=str(limit_price),
                triggerPrice=str(sl_price),
                triggerDirection=trigger_direction,
                reduceOnly=True,
                closeOnTrigger=True,
            )
            if res.get("retCode") == 0 and res.get("result"):
                ord_id = res["result"].get("orderId")
                trade["sl_info"] = {
                    "ordId": ord_id,
                    "price_filled": None,
                    "size": current_fill_size,
                    "size_filled": None,
                    "status": "active",
                    "breakeven": False,
                }
                logging.info(f"✅ {instrument_id} Bybit stop loss (conditional) placed at {sl_price}, orderId={ord_id}")
                return True
            logging.warning(f"{instrument_id} Bybit place_stop_loss_order failed: {res.get('retMsg')}")
            return False
        except Exception as e:
            logging.error(f"{instrument_id} Bybit place_stop_loss_order error: {e}")
            return False

    async def stop_breakeven(self, trades_dict: dict, instrument_id: str, trade_info: dict, instrument_info: tuple) -> bool:
        """
        Перевод стоп-лосса в безубыток.
        Аналог OKX.stop_breakeven, но через amend_order Bybit.
        """
        max_leverage, tick_size, min_order_qty, qty_step, min_notional_value = instrument_info
        trade = trades_dict[trade_info["message_id"]]
        sl_info = trade.get("sl_info")
        if not sl_info:
            logging.warning("%s Bybit stop_breakeven: no sl_info", instrument_id)
            return False

        side_to_close = "Sell" if trade_info["side"] == "long" else "Buy"

        # Определяем цену входа: сначала реально заполненную, потом fallback на entry
        entry_list = trade.get("entry") or []
        entry_info = trade.get("entry_info")
        entry_price = None
        if isinstance(entry_info, dict):
            entry_price = entry_info.get("price_filled")
        elif isinstance(entry_info, list) and entry_info:
            entry_price = entry_info[0].get("price_filled")
        if not entry_price:
            if entry_list:
                entry_price = float(entry_list[0])
            else:
                logging.warning("%s Bybit stop_breakeven: no entry price", instrument_id)
                return False
        entry_price = float(entry_price)

        # Чуть в плюс относительно цены входа 
        if side_to_close == "Sell":
            breakeven_price = self.round_tick(entry_price * 1.005, tick_size)
            new_trigger = self.round_tick(breakeven_price * 1.002, tick_size)
        else:
            breakeven_price = self.round_tick(entry_price * 0.995, tick_size)
            new_trigger = self.round_tick(breakeven_price * 0.998, tick_size)

        # Стоп теперь лимитный, поэтому вместе с triggerPrice двигаем и лимит-цену
        if side_to_close == "Sell":
            new_limit_price = self.round_tick(new_trigger * 0.999, tick_size)
        else:
            new_limit_price = self.round_tick(new_trigger * 1.001, tick_size)

        try:
            res = await asyncio.to_thread(
                self.trade_session.amend_order,
                category="linear",
                symbol=instrument_id,
                orderId=sl_info["ordId"],
                triggerPrice=str(new_trigger),
                price=str(new_limit_price),
            )
        except Exception as e:
            logging.error("%s Bybit stop_breakeven amend_order error: %s", instrument_id, e)
            return False

        if res.get("retCode") == 0:
            trade["sl_info"]["breakeven"] = True
            logging.info("✅ %s Bybit stop breakeven updated: trigger=%s", instrument_id, new_trigger)
            return True

        logging.warning("%s Bybit stop_breakeven failed: %s", instrument_id, res.get("retMsg"))
        return False

    async def close_entire_position(self, trades_dict: dict, instrument_id: str, trade_info: dict, instrument_info: tuple) -> bool:
        """
        Полное закрытие позиции по рынку.
        Аналог OKX.close_entire_position, но через Bybit place_order.
        """
        max_leverage, tick_size, min_order_qty, qty_step, min_notional_value = instrument_info
        trade = trades_dict[trade_info["message_id"]]
        current_fill_size = float(trade["current_fill_size"])
        side_to_close = "Sell" if trade_info["side"] == "long" else "Buy"

        try:
            res = await asyncio.to_thread(
                self.trade_session.place_order,
                category="linear",
                symbol=instrument_id,
                side=side_to_close,
                orderType="Market",
                qty=str(self.round_tick(current_fill_size, qty_step)),
                reduceOnly=True,
            )
        except Exception as e:
            logging.error("%s Bybit close_entire_position error: %s", instrument_id, e)
            return False

        if res.get("retCode") == 0 and res.get("result"):
            ord_id = res["result"].get("orderId")
            trade["status"] = "closed"
            logging.info("✅ %s Bybit closed entire position orderId=%s", instrument_id, ord_id)
            return True

        logging.warning("%s Bybit close_entire_position failed: %s", instrument_id, res.get("retMsg"))
        return False

    
if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()

    async def _run_tests():
        bybit_trade = BybitTrade(
            api_key=os.getenv("BYBIT_API_KEY"),
            api_secret=os.getenv("BYBIT_API_SECRET"),
            balance=float(os.getenv("TRADING_BALANCE", "10")),
        )
        symbol = "UNIUSDT"
        available_info = await bybit_trade.get_instrument_info(symbol=symbol)
        if available_info[0] is None:
            logging.error("Instrument %s not available", symbol)
            return

        trades_dict = {}
        trade_info = {
            "action": "place_new_order",
            "trader": "trader #A",
            "coin": symbol,
            "side": "short",
            "entry": [4],
            "tp": [3.92, 3.84, 3.76],
            "sl": 4.04,
            "risk": "1%",
            "message_id": 2066,
        }

        # 1. place_new_order (limit)
        logging.info("--- Test place_new_order ---")
        ok = await bybit_trade.place_new_order(trades_dict, symbol, trade_info, available_info)
        logging.info("place_new_order: %s", ok)
        pprint(trades_dict, indent=2, width=80, sort_dicts=False)

        time.sleep(2)
        # 2. enter_now (market)
        logging.info("--- Test enter_now ---")
        ok = await bybit_trade.enter_now(trades_dict, symbol, trade_info, available_info)
        logging.info("enter_now: %s", ok)
        pprint(trades_dict, indent=2, width=80, sort_dicts=False)

        trades_dict[trade_info["message_id"]]["current_fill_size"] = 2.6
        pprint(trades_dict, indent=2, width=80, sort_dicts=False)
        time.sleep(2)

        # 3. place_stop_loss_order (conditional)
        logging.info("--- Test place_stop_loss_order ---")
        ok = await bybit_trade.place_stop_loss_order(trades_dict, symbol, trade_info, available_info)
        logging.info("place_stop_loss_order: %s", ok)
        pprint(trades_dict, indent=2, width=80, sort_dicts=False)

    # asyncio.run(_run_tests())



