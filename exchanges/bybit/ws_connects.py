import logging
import websockets
import asyncio
import json
import hmac
import time
import hashlib
import os as _os

_logs_dir = _os.path.normpath(_os.path.join(_os.path.dirname(__file__), "..", "..", "logs"))
_os.makedirs(_logs_dir, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(_os.path.join(_logs_dir, "logarik.log"), encoding="utf-8"),
        logging.StreamHandler(),
    ],
)


class WebSocketClient:
    def __init__(self, api_key: str, secret_key: str, orders_queue: asyncio.Queue):
        self.orders_queue = orders_queue
        self.private_url = "wss://stream.bybit.com/v5/private"
        self.api_key = api_key
        self.secret_key = secret_key
        self.private_ws = None
        self.running = True
        self.reconnect_delay = 2
        self.private_task = None

    async def connect_private(self):
        while self.running:
            try:
                async with websockets.connect(self.private_url) as ws:
                    self.private_ws = ws
                    await self.login()
                    await self.subscribe_private()
                    await self.listen_private()
            except Exception as e:
                if not self.running:
                    break
                logging.warning(f"❗️ Bybit private WS error: {e}, reconnecting in {self.reconnect_delay}s")
                await asyncio.sleep(self.reconnect_delay)

    async def subscribe_private(self):
        msg = {
            "op": "subscribe",
            "args": ["position", "order"],
        }
        await self.private_ws.send(json.dumps(msg))
        logging.info("✅ Bybit subscribed to private position & order")

    async def login(self):
        """
        Авторизация по примерe из https://bybit-exchange.github.io/docs/v5/ws/connect
        """
        expires = int((time.time() + 10) * 1000)
        message = f"GET/realtime{expires}"
        signature = hmac.new(
            self.secret_key.encode("utf-8"),
            message.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).hexdigest()

        login_msg = {
            "op": "auth",
            "args": [self.api_key, expires, signature],
        }
        await self.private_ws.send(json.dumps(login_msg))

        async for raw in self.private_ws:
            data = json.loads(raw)
            # Успешный ответ auth
            if data.get("op") == "auth":
                if data.get("success") is True:
                    logging.info("✅ Bybit private WS auth successful")
                    break
                raise Exception(f"❗️ Bybit auth failed: {data}")

    async def listen_private(self):
        async for raw in self.private_ws:
            data = json.loads(raw)
            topic = data.get("topic")
            if topic in ("position", "order"):
                payload = data.get("data", [])
                if payload:
                    logging.info(f"🌂🌂🌂 Bybit WS update {topic}: {payload}")
                    await self.orders_queue.put(data)

    async def start(self):
        try:
            self.running = True
            self.private_task = asyncio.create_task(self.connect_private())
            await self.private_task
        except Exception as e:
            logging.warning(e)

    async def shutdown(self):
        logging.info("🌀 Shutting down Bybit WebSocket client...")
        self.running = False
        if self.private_ws:
            await self.private_ws.close()
        if self.private_task:
            self.private_task.cancel()
            try:
                await self.private_task
            except asyncio.CancelledError:
                pass
        logging.info("✅ Bybit WebSocket client stopped")


if __name__ == "__main__":
    import os
    from dotenv import load_dotenv
    load_dotenv()
    async def _test():
        api_key = os.getenv("BYBIT_API_KEY", "")
        api_secret = os.getenv("BYBIT_API_SECRET", "")
        if not api_key or not api_secret:
            logging.error("BYBIT_API_KEY / BYBIT_API_SECRET are not set in environment")
            return

        orders_queue: asyncio.Queue = asyncio.Queue()
        ws_client = WebSocketClient(api_key=api_key, secret_key=api_secret, orders_queue=orders_queue)

        async def _consumer():
            while True:
                msg = await orders_queue.get()

        # Запускаем WS и consumer в фоне
        ws_task = asyncio.create_task(ws_client.start())
        consumer_task = asyncio.create_task(_consumer())

        try:
            logging.info("⏱ Bybit WS test: running for 60 seconds...")
            await asyncio.sleep(60)
        finally:
            await ws_client.shutdown()
            ws_task.cancel()
            try:
                await ws_task
            except asyncio.CancelledError:
                pass
            consumer_task.cancel()
            try:
                await consumer_task
            except asyncio.CancelledError:
                pass

    asyncio.run(_test())

