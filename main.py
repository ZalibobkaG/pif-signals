"""
Точка входа. Запускает:
- таск с получением тг сообщений (TgChannelMonitor) + очередь в AI
- таск с запросами/ответами нейронки (WorkWithAI.listen)
- таск с логикой обработки запросов от нейронки и выставление ордеров (заглушка)
- таск с вебсокет потоком и мониторингом позиций (заглушка)
- таск с тг ботом (заглушка)
"""

import asyncio
import os
import sys
from dotenv import load_dotenv
load_dotenv()
from loguru import logger
from config import BALANCE, BACKUP_INTERVAL_SECONDS, BACKUP_MAX_FILES
from utils.tg_channel_monitor import TgChannelMonitor
from utils.work_with_ai import WorkWithAI
from exchanges.okx.okx_strategy import okx_strategy_pool
from exchanges.okx.trade import OkxTrade
from exchanges.okx.ws_connects import WebSocketClient
from exchanges.bybit.bybit_strategy import bybit_strategy_pool
from exchanges.bybit.ws_connects import WebSocketClient as BybitWebSocketClient
from exchanges.bybit.trade import BybitTrade
from tg_bot import bot as tg_bot
from tg_bot import context as tg_context
from utils.trades_backup import backup_trades_dict_loop


# Логирование в консоль и в файл (loguru)
_base_dir = os.path.dirname(os.path.abspath(__file__))
_logs_dir = os.path.join(_base_dir, "logs")
os.makedirs(_logs_dir, exist_ok=True)
_log_file = os.path.join(_logs_dir, "logarik.log")
_log_fmt = "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} | {message}"
logger.remove()
logger.add(sys.stdout, format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}:{function}:{line}</cyan> | <level>{message}</level>")
logger.add(_log_file, encoding="utf-8", format=_log_fmt)


trades_dict = {}
tg_context.trades_dict = trades_dict


async def full_shutdown(okx_ws, bybit_ws):
    """По /stop в тг боте: закрыть ws, отменить все таски, выйти."""
    logger.info("🛑 Full shutdown: останавливаю WebSocket и все таски...")
    await okx_ws.shutdown()
    await bybit_ws.shutdown()
    current_task = asyncio.current_task()
    tasks = [t for t in asyncio.all_tasks() if t is not current_task]
    for t in tasks:
        t.cancel()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for r in results:
        if isinstance(r, Exception) and not isinstance(r, asyncio.CancelledError):
            logger.exception(f"Task exception during shutdown: {r}")
    logger.info("✅ Full shutdown completed: ws closed, tasks canceled.")


async def main():
    logger.info("Main started.")
    tg_messages_queue = asyncio.Queue()
    ai_response_queue = asyncio.Queue()
    user_choice_queue = asyncio.Queue()
    bybit_queue = asyncio.Queue()
    okx_ws_queue = asyncio.Queue()
    bybit_ws_queue = asyncio.Queue()

    api_key = os.environ.get("OKX_API_KEY", "")
    api_secret = os.environ.get("OKX_SECRET_KEY", "")
    passphrase = os.environ.get("OKX_PASSPHRASE", "")
    okx_trade = OkxTrade(api_key=api_key, api_secret=api_secret, api_passphrase=passphrase, balance=BALANCE)
    okx_ws = WebSocketClient(api_key, api_secret, passphrase, okx_ws_queue)

    bybit_api_key = os.environ.get("BYBIT_API_KEY", "")
    bybit_api_secret = os.environ.get("BYBIT_API_SECRET", "")
    bybit_ws = BybitWebSocketClient(bybit_api_key, bybit_api_secret, bybit_ws_queue) if (bybit_api_key and bybit_api_secret) else None
    bybit_trade = BybitTrade(api_key=bybit_api_key, api_secret=bybit_api_secret, balance=BALANCE)

    tg_context.user_choice_queue = user_choice_queue
    monitor = TgChannelMonitor(messages_queue=tg_messages_queue)
    ai = WorkWithAI(messages_queue=tg_messages_queue, ai_response_queue=ai_response_queue)
    shutdown_cb = lambda: full_shutdown(okx_ws, bybit_ws)

    async def _run_bot():
        try:
            await tg_bot.run_with_shutdown(shutdown_cb)
        except Exception as e:
            logger.exception("TG bot task failed: %s", e)

    tasks = [
        asyncio.create_task(_run_bot()),
        asyncio.create_task(monitor.run()),
        asyncio.create_task(ai.listen()),
        asyncio.create_task(backup_trades_dict_loop(trades_dict, _logs_dir, BACKUP_INTERVAL_SECONDS, keep_last=BACKUP_MAX_FILES)),
        asyncio.create_task(okx_strategy_pool(ai_response_queue, user_choice_queue, trades_dict, bybit_queue, okx_trade, bybit_trade, okx_ws_queue)),
        asyncio.create_task(bybit_strategy_pool(bybit_queue, trades_dict, bybit_ws_queue, bybit_trade)),
        asyncio.create_task(okx_ws.connect_private()),
        asyncio.create_task(bybit_ws.connect_private()),
    ]
    logger.info("All tasks created (tg_bot first, then monitor, ai, okx, bybit, ws). Waiting...")
    await asyncio.sleep(0.2)
    
    try:
        await asyncio.wait(tasks)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.exception(f"Ошибка в main: {e}")


if __name__ == "__main__":
    asyncio.run(main())
