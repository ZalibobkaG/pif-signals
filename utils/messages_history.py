"""
Сохранение истории сообщений с канала в messages_history.json.
Та же логика что в tg_channel_monitor: trader#3 пропускаем, реплаи и ссылки t.me обрабатываем.
"""
import asyncio
import os
import json
import re
import logging

from dotenv import load_dotenv
from telethon import TelegramClient, events

load_dotenv()

_BASE_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
LOGS_DIR = os.path.join(_BASE_DIR, "logs")
SESSIONS_DIR = os.path.join(_BASE_DIR, "tg_sessions")
HISTORY_FILE = os.path.join(LOGS_DIR, "messages_history.json")

_log_fmt = '%(asctime)s - %(levelname)s - %(message)s'
os.makedirs(LOGS_DIR, exist_ok=True)
os.makedirs(SESSIONS_DIR, exist_ok=True)
logging.basicConfig(level=logging.INFO, format=_log_fmt)
logging.getLogger("telethon").setLevel(logging.WARNING)


def _extract_tme_id(text):
    if not text:
        return None
    match = re.search(r"t\.me/c/\d+/(\d+)", text)
    if match:
        return int(match.group(1))
    return None


class MessagesHistory:
    def __init__(
        self,
        api_id: int = None,
        api_hash: str = "",
        session_name: str = "tg_monitor_session",
        channel: str = "",
    ):
        self.api_id = api_id or int(os.environ.get("TG_API_ID", "0"))
        self.api_hash = api_hash or os.environ.get("TG_API_HASH", "")
        self.session_name = session_name or os.environ.get("TG_SESSION", "tg_monitor_session")
        self.channel = (channel or os.environ.get("TG_CHANNEL", "")).strip()
        self.history_file = HISTORY_FILE
        self.client = None
        self.processed_ids = set()
        self.messages_data = {}

    def _load_messages_data(self):
        if os.path.exists(self.history_file):
            try:
                with open(self.history_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.messages_data = {}
                for k, v in data.items():
                    root_id = int(k)
                    if isinstance(v, str):
                        self.messages_data[root_id] = v
                    elif isinstance(v, dict):
                        self.messages_data[root_id] = {int(k2): v2 for k2, v2 in v.items()}
            except Exception as e:
                logging.error(f"Ошибка загрузки JSON: {e}")
                self.messages_data = {}
                self._save_messages_data()
        else:
            self.messages_data = {}
            self._save_messages_data()

    def _save_messages_data(self):
        os.makedirs(LOGS_DIR, exist_ok=True)
        json_data = {}
        for k, v in self.messages_data.items():
            if isinstance(v, str):
                json_data[str(k)] = v
            elif isinstance(v, dict):
                json_data[str(k)] = {str(k2): v2 for k2, v2 in v.items()}
        with open(self.history_file, "w", encoding="utf-8") as f:
            json.dump(json_data, f, ensure_ascii=False, indent=2)

    async def _find_root_message_id(self, msg):
        visited = set()
        current_id = msg.id
        
        if msg.reply_to and msg.reply_to.reply_to_msg_id:
            current_id = msg.reply_to.reply_to_msg_id
        
        while True:
            if current_id in visited:
                return current_id
            visited.add(current_id)
            try:
                result = await self.client.get_messages(msg.peer_id, ids=current_id)
                if isinstance(result, list):
                    current_msg = result[0] if result else None
                else:
                    current_msg = result
                    
                if not current_msg:
                    return current_id
                    
                tme_id = _extract_tme_id(current_msg.text)
                if tme_id:
                    return tme_id
                    
                if current_msg.reply_to and current_msg.reply_to.reply_to_msg_id:
                    current_id = current_msg.reply_to.reply_to_msg_id
                else:
                    return current_id
            except Exception:
                return current_id

    async def _save_message(self, msg, is_edit=False):
        text = msg.text or "[медиа/без текста]"
        if "trader#3" in text.lower() or "trader #3" in text.lower() or "trader #4" in text.lower() or "trader #1" in text.lower() or "trader #2" in text.lower() or "trader#4" in text.lower() or "trader#1" in text.lower() or "trader#2" in text.lower():
            return
        if not is_edit and msg.id in self.processed_ids:
            return
        if is_edit and msg.id not in self.processed_ids:
            return

        root_id = await self._find_root_message_id(msg)
        is_root = not msg.reply_to and not _extract_tme_id(text)

        if is_edit:
            old_text = None
            if root_id in self.messages_data:
                data = self.messages_data[root_id]
                if isinstance(data, str):
                    if root_id == msg.id:
                        old_text = data
                else:
                    old_text = data.get(msg.id)
            if old_text is not None and old_text == text:
                return

        if is_root and root_id == msg.id:
            if root_id not in self.messages_data:
                self.messages_data[msg.id] = text
            elif isinstance(self.messages_data[root_id], dict):
                self.messages_data[root_id][msg.id] = text
            else:
                self.messages_data[root_id] = text
        else:
            if root_id not in self.messages_data:
                try:
                    result = await self.client.get_messages(msg.peer_id, ids=root_id)
                    if isinstance(result, list):
                        root_msg = result[0] if result else None
                    else:
                        root_msg = result
                        
                    if root_msg and root_msg.text:
                        self.messages_data[root_id] = {root_id: root_msg.text}
                        self.processed_ids.add(root_id)
                    else:
                        logging.warning(f"Не удалось получить root сообщение {root_id} для сообщения {msg.id}")
                        self.messages_data[root_id] = {root_id: "[root не найден]"}
                        self.processed_ids.add(root_id)
                except Exception as e:
                    logging.warning(f"Ошибка при получении root сообщения {root_id} для сообщения {msg.id}: {e}")
                    self.messages_data[root_id] = {root_id: "[root не найден]"}
                    self.processed_ids.add(root_id)
            elif isinstance(self.messages_data[root_id], str):
                root_text = self.messages_data[root_id]
                self.messages_data[root_id] = {root_id: root_text}
            if not isinstance(self.messages_data[root_id], dict):
                self.messages_data[root_id] = {root_id: self.messages_data[root_id]}
            self.messages_data[root_id][msg.id] = text

        self._save_messages_data()
        prefix = "[EDIT]" if is_edit else "[NEW]"
        timestamp = msg.date.strftime("%Y-%m-%d %H:%M:%S")
        reply_info = f" | ROOT: {root_id}" if root_id != msg.id else ""
        logging.info(f"{prefix} {timestamp} | ID: {msg.id}{reply_info} | {text[:50]}")
        self.processed_ids.add(msg.id)

    async def run(self):
        self._load_messages_data()
        self.processed_ids.update(self.messages_data.keys())
        for root_id, data in self.messages_data.items():
            if isinstance(data, dict):
                self.processed_ids.update(data.keys())

        session_path = os.path.join(SESSIONS_DIR, self.session_name)
        
        self.client = TelegramClient(
            session_path,
            self.api_id,
            self.api_hash,
        )

        await self.client.start()
        
        try:
            if self.channel.lstrip('-').isdigit():
                channel_id = int(self.channel)
            else:
                channel_id = self.channel
            
            try:
                channel_entity = await self.client.get_entity(channel_id)
                logging.info(f"История канала: {getattr(channel_entity, 'title', channel_id)}")
            except Exception as e:
                logging.warning(f"Не удалось получить канал {channel_id}: {e}")
                async for dialog in self.client.iter_dialogs():
                    if dialog.id == channel_id or (hasattr(dialog.entity, 'username') and dialog.entity.username == channel_id.lstrip('@')):
                        channel_entity = dialog.entity
                        logging.info(f"Найден канал через диалоги: {getattr(channel_entity, 'title', channel_id)}")
                        break
                else:
                    logging.error(f"Канал {channel_id} не найден")
                    return
        except Exception as e:
            logging.error(f"Ошибка при получении канала: {e}")
            return

        logging.info("Загружаю всю историю канала...")
        all_messages = []
        async for msg in self.client.iter_messages(channel_entity):
            if msg.id not in self.processed_ids:
                all_messages.append(msg)
        logging.info(f"Собрано {len(all_messages)} новых сообщений, сортирую по ID (от старых к новым)...")
        all_messages.sort(key=lambda m: m.id)
        
        count = 0
        for msg in all_messages:
            try:
                await self._save_message(msg, is_edit=False)
                count += 1
                if count % 100 == 0:
                    logging.info(f"Обработано {count}/{len(all_messages)} сообщений...")
            except Exception as e:
                logging.error(f"Ошибка при сохранении сообщения {msg.id}: {e}")
        logging.info(f"История загружена: обработано {count} новых сообщений")
        await self.client.disconnect()


if __name__ == "__main__":
    history = MessagesHistory()
    asyncio.run(history.run())
