import logging
import websockets
import asyncio
import json
import hmac
import time 
import hashlib
import base64

import os as _os
_logs_dir = _os.path.normpath(_os.path.join(_os.path.dirname(__file__), "..", "..", "logs"))
_os.makedirs(_logs_dir, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(_os.path.join(_logs_dir, "logarik.log"), encoding="utf-8"),
        logging.StreamHandler()
    ]
)

class WebSocketClient:
	def __init__(self, api_key, secret_key, passphrase, orders_queue: asyncio.Queue):
		self.orders_queue = orders_queue
		self.private_url = "wss://ws.okx.com:8443/ws/v5/private"
		self.api_key = api_key
		self.secret_key = secret_key
		self.passphrase = passphrase
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
				logging.warning(f"❗️ OKX Private WS error: {e}, reconnecting in {self.reconnect_delay}s")
				await asyncio.sleep(self.reconnect_delay)

	async def subscribe_private(self):
		msg = {
			"op": "subscribe",
			"args": [{"channel": "orders", "instType": "SWAP"}, {"channel": "orders-algo", "instType": "SWAP"}]
		}
		await self.private_ws.send(json.dumps(msg))
		logging.info(f"✅ OKX Subscribed to private orders")

	async def login(self):
		timestamp = str(time.time())
		method = "GET"
		request_path = "/users/self/verify"
		message = timestamp + method + request_path
		mac = hmac.new(self.secret_key.encode(), message.encode(), hashlib.sha256)
		sign = base64.b64encode(mac.digest()).decode()

		login_msg = {
			"op": "login",
			"args": [{
				"apiKey": self.api_key,
				"passphrase": self.passphrase,
				"timestamp": timestamp,
				"sign": sign
			}]
		}
		await self.private_ws.send(json.dumps(login_msg))

		async for msg in self.private_ws:
			data = json.loads(msg)
			if data.get("event") == "login" and data.get("code") == "0":
				logging.info("✅ OKX Login successful")
				break
			elif data.get("event") == "login" and data.get("code") != "0":
				raise Exception(f"❗️ OKX Login failed: {data}")

	async def listen_private(self):
		async for msg in self.private_ws:
			data = json.loads(msg)
			if "arg" in data and data["arg"].get("channel") in ["orders", "orders-algo"]:
				orders = data.get("data", [])
				# Only enqueue when there are actual order updates
				if orders:
					# print(f"Private orders update: {orders}")
					# Log all order states from the update
					states = [order.get("state") for order in orders]
					logging.info(f"🚦🚦🚦 WS update {states}: {orders}")
					await self.orders_queue.put(data)

	async def start(self):
		try:
			self.running = True
			self.private_task = asyncio.create_task(self.connect_private())
			await self.private_task
		except Exception as e:
			logging.warning(e)

	async def shutdown(self):
		logging.info("🌀 OKX Shutting down WebSocket manager...")
		self.running = False
		if self.private_ws:
			await self.private_ws.close()
		if self.private_task:
			self.private_task.cancel()
			try:
				await self.private_task
			except asyncio.CancelledError:
				pass
		logging.info("✅ OKX WebSocket manager stopped")
