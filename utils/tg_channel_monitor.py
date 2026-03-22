"""
Мониторинг закрытого ТГ-канала: новые сообщения и редактирования (асинхронно).
"""
import asyncio
import os
import json
import re

from dotenv import load_dotenv
from loguru import logger
from telethon import TelegramClient, events

load_dotenv()

_BASE_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
LOGS_DIR = os.path.join(_BASE_DIR, "logs")
SESSIONS_DIR = os.path.join(_BASE_DIR, "tg_sessions")
DEFAULT_MESSAGES_FILE = os.path.join(LOGS_DIR, "channel_messages.json")

os.makedirs(LOGS_DIR, exist_ok=True)
os.makedirs(SESSIONS_DIR, exist_ok=True)

def _extract_tme_id(text):
    """Извлекает первую ссылку t.me из текста, возвращает message_id."""
    if not text:
        return None
    match = re.search(r"t\.me/c/\d+/(\d+)", text)
    if match:
        return int(match.group(1))
    return None


def _extract_all_tme_ids(text):
    """Извлекает все ссылки t.me из текста, возвращает список message_id."""
    if not text:
        return []
    matches = re.findall(r"t\.me/c/\d+/(\d+)", text)
    return [int(mid) for mid in matches]


class TgChannelMonitor:
    def __init__(
        self,
        api_id: int = None,
        api_hash: str = "",
        session_name: str = "tg_monitor_session",
        channel: str = "",
        messages_file: str = None,
        messages_queue: asyncio.Queue = None,
        ):
        self.api_id = api_id or int(os.environ.get("TG_API_ID", "0"))
        self.api_hash = api_hash or os.environ.get("TG_API_HASH", "")
        self.session_name = session_name or os.environ.get("TG_SESSION", "tg_session")
        self.channel = (channel or os.environ.get("TG_CHANNEL", "")).strip()
        self.messages_file = messages_file or DEFAULT_MESSAGES_FILE
        self.messages_queue = messages_queue
        self.client = None
        self.processed_ids = set()
        self.messages_data = {}

    def _load_messages_data(self):
        if os.path.exists(self.messages_file):
            try:
                with open(self.messages_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.messages_data = {}
                for k, v in data.items():
                    root_id = int(k)
                    if isinstance(v, str):
                        self.messages_data[root_id] = v
                    elif isinstance(v, dict):
                        self.messages_data[root_id] = {int(k2): v2 for k2, v2 in v.items()}
            except Exception as e:
                logger.error(f"Ошибка загрузки JSON: {e}")
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
        with open(self.messages_file, "w", encoding="utf-8") as f:
            json.dump(json_data, f, ensure_ascii=False, indent=2)

    def _thread_text(self, root_id):
        data = self.messages_data.get(root_id)
        if data is None:
            return None
        if isinstance(data, str):
            return data
        parts = [data[k] for k in sorted(data.keys())]
        return "\n\n---\n\n".join(parts)

    async def _find_root_message_id(self, msg):
        visited = set()
        current_id = msg.id
        
        # Если есть прямой реплай, начинаем с него
        if msg.reply_to and msg.reply_to.reply_to_msg_id:
            current_id = msg.reply_to.reply_to_msg_id
        
        while True:
            if current_id in visited:
                return current_id
            visited.add(current_id)
            try:
                # get_messages может вернуть список, берем первый элемент
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
        if "trader#3" in text.lower() or "trader #3" in text.lower():
            return
        
        # Проверяем, есть ли в тексте несколько ссылок
        all_link_ids = _extract_all_tme_ids(text)
        
        # Если есть несколько ссылок, обрабатываем каждую отдельно (пропускаем проверку processed_ids)
        if len(all_link_ids) > 1:
            logger.info(f"Обнаружено {len(all_link_ids)} ссылок в сообщении {msg.id}, обрабатываю каждую отдельно")
            for link_id in all_link_ids:
                await self._save_message_for_link_id(msg, text, link_id, is_edit)
            # Помечаем сообщение как обработанное после обработки всех ссылок
            self.processed_ids.add(msg.id)
            return
        
        # Обычная обработка (одна ссылка или нет ссылок) - проверяем processed_ids
        if not is_edit and msg.id in self.processed_ids:
            logger.info(f"skip by processed_ids (new): msg.id={msg.id}")
            return
        if is_edit and msg.id not in self.processed_ids:
            # Бывает без «реального» редактирования: только MessageEdited без NewMessage
            # (превью ссылок, порядок доставки, рестарт бота до прихода NewMessage и т.д.)
            logger.info(f"edit без записи в processed_ids — обрабатываем как первое появление: msg.id={msg.id}")
            is_edit = False

        # Обычная обработка (одна ссылка или нет ссылок)
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
                # редактирование корня, сохранённого как строка (например было "[медиа/без текста]")
                self.messages_data[root_id] = text
        else:
            if root_id not in self.messages_data:
                try:
                    # get_messages может вернуть список, берем первый элемент
                    result = await self.client.get_messages(msg.peer_id, ids=root_id)
                    if isinstance(result, list):
                        root_msg = result[0] if result else None
                    else:
                        root_msg = result
                        
                    if root_msg and root_msg.text:
                        self.messages_data[root_id] = {root_id: root_msg.text}
                        self.processed_ids.add(root_id)
                    else:
                        logger.warning(f"Не удалось получить root сообщение {root_id} для сообщения {msg.id}")
                        # Создаем запись с пустым текстом root, чтобы сохранить реплай
                        self.messages_data[root_id] = {root_id: "[root не найден]"}
                        self.processed_ids.add(root_id)
                except Exception as e:
                    logger.warning(f"Ошибка при получении root сообщения {root_id} для сообщения {msg.id}: {e}")
                    # Создаем запись с пустым текстом root, чтобы сохранить реплай
                    self.messages_data[root_id] = {root_id: "[root не найден]"}
                    self.processed_ids.add(root_id)
            elif isinstance(self.messages_data[root_id], str):
                root_text = self.messages_data[root_id]
                self.messages_data[root_id] = {root_id: root_text}
            if not isinstance(self.messages_data[root_id], dict):
                self.messages_data[root_id] = {root_id: self.messages_data[root_id]}
            self.messages_data[root_id][msg.id] = text

        self._save_messages_data()
        full_text = self._thread_text(root_id)
        # В нейронку не передаём сообщения без текста (только медиа)
        if full_text and full_text.strip() and full_text.strip() != "[медиа/без текста]" and self.messages_queue is not None:
            self.messages_queue.put_nowait({"text": full_text, "root_id": root_id})

        prefix = "[EDIT]" if is_edit else "[NEW]"
        timestamp = msg.date.strftime("%Y-%m-%d %H:%M:%S")
        reply_info = f" | ROOT: {root_id}" if root_id != msg.id else ""
        logger.info(f"{prefix} {timestamp} | ID: {msg.id}{reply_info} | {text[:50]}")
        self.processed_ids.add(msg.id)

    async def _save_message_for_link_id(self, msg, text, link_id, is_edit=False):
        """Обрабатывает сообщение для конкретной ссылки (link_id = message_id из ссылки)."""
        root_id = link_id
        
        # Получаем root сообщение по ссылке
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
                    logger.warning(f"Не удалось получить root сообщение {root_id} для ссылки в сообщении {msg.id}")
                    self.messages_data[root_id] = {root_id: "[root не найден]"}
                    self.processed_ids.add(root_id)
            except Exception as e:
                logger.warning(f"Ошибка при получении root сообщения {root_id} для ссылки в сообщении {msg.id}: {e}")
                self.messages_data[root_id] = {root_id: "[root не найден]"}
                self.processed_ids.add(root_id)
        
        # Преобразуем в dict если нужно
        if isinstance(self.messages_data[root_id], str):
            root_text = self.messages_data[root_id]
            self.messages_data[root_id] = {root_id: root_text}
        if not isinstance(self.messages_data[root_id], dict):
            self.messages_data[root_id] = {root_id: self.messages_data[root_id]}
        
        # Проверяем, не было ли это сообщение уже добавлено для этого root_id (для редактирования)
        if is_edit:
            old_text = self.messages_data[root_id].get(msg.id)
            if old_text is not None and old_text == text:
                return
        
        # Добавляем текущее сообщение как реплай к этому root
        self.messages_data[root_id][msg.id] = text
        
        self._save_messages_data()
        
        # Отправляем в очередь для AI отдельно для каждой ссылки
        full_text = self._thread_text(root_id)
        if full_text and full_text.strip() and full_text.strip() != "[медиа/без текста]" and self.messages_queue is not None:
            self.messages_queue.put_nowait({"text": full_text, "root_id": root_id})
        
        prefix = "[EDIT]" if is_edit else "[NEW]"
        timestamp = msg.date.strftime("%Y-%m-%d %H:%M:%S")
        logger.info(f"{prefix} {timestamp} | ID: {msg.id} | LINK: {link_id} | {text[:50]}")

    async def run(self):
        if self.messages_queue is None:
            self.messages_queue = asyncio.Queue()

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
        
        # Получаем канал для проверки
        try:
            if self.channel.lstrip('-').isdigit():
                channel_id = int(self.channel)
            else:
                channel_id = self.channel
            
            # Пробуем получить канал для проверки
            try:
                channel_entity = await self.client.get_entity(channel_id)
                logger.info(f"Мониторинг канала: {getattr(channel_entity, 'title', channel_id)}")
            except Exception as e:
                logger.warning(f"Не удалось получить канал {channel_id}: {e}")
                # Пробуем найти через диалоги
                async for dialog in self.client.iter_dialogs():
                    if dialog.id == channel_id or (hasattr(dialog.entity, 'username') and dialog.entity.username == channel_id.lstrip('@')):
                        channel_entity = dialog.entity
                        logger.info(f"Найден канал через диалоги: {getattr(channel_entity, 'title', channel_id)}")
                        break
                else:
                    logger.error(f"Канал {channel_id} не найден")
                    return
        except Exception as e:
            logger.error(f"Ошибка при получении канала: {e}")
            return

        @self.client.on(events.NewMessage(chats=channel_entity))
        async def on_new_message(event):
            logger.info(f"Получено новое сообщение: ID={event.message.id}, reply_to={event.message.reply_to}")
            try:
                await self._save_message(event.message, is_edit=False)
            except Exception as e:
                logger.error(f"Ошибка при сохранении сообщения {event.message.id}: {e}")
                import traceback
                logger.error(traceback.format_exc())

        @self.client.on(events.MessageEdited(chats=channel_entity))
        async def on_edit(event):
            if not event.message.out:
                logger.info(f"Получено редактирование сообщения: ID={event.message.id}, reply_to={event.message.reply_to}")
                try:
                    await self._save_message(event.message, is_edit=True)
                except Exception as e:
                    logger.error(f"Ошибка при сохранении редактирования {event.message.id}: {e}")
                    import traceback
                    logger.error(traceback.format_exc())

        logger.info("Хендлер зарегистрирован, ожидание сообщений...")
        await self.client.run_until_disconnected()


if __name__ == "__main__":
    monitor = TgChannelMonitor()
    asyncio.run(monitor.run())
