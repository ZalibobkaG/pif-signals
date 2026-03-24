"""
Мониторинг Telegram forum-группы по выбранным топикам:
- новые сообщения
- редактирования
"""
import asyncio
import os
import json
import re

from dotenv import load_dotenv
from loguru import logger
from telethon import TelegramClient, events
from telethon.tl.functions.messages import GetForumTopicsRequest


load_dotenv()

_BASE_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
LOGS_DIR = os.path.join(_BASE_DIR, "logs")
SESSIONS_DIR = os.path.join(_BASE_DIR, "tg_sessions")
DEFAULT_MESSAGES_FILE = os.path.join(LOGS_DIR, "group_topics_messages.json")

os.makedirs(LOGS_DIR, exist_ok=True)
os.makedirs(SESSIONS_DIR, exist_ok=True)


def _extract_tme_id(text):
    """Извлекает первую ссылку t.me из текста, возвращает message_id."""
    if not text:
        return None
    # Поддержка обеих форм:
    # - t.me/c/<chat>/<msg_id>
    # - t.me/c/<chat>/<topic_id>/<msg_id>
    match = re.search(r"t\.me/c/\d+/(?:\d+/)?(\d+)", text)
    if match:
        return int(match.group(1))
    return None


def _extract_all_tme_ids(text):
    """Извлекает все ссылки t.me из текста, возвращает список message_id."""
    if not text:
        return []
    matches = re.findall(r"t\.me/c/\d+/(?:\d+/)?(\d+)", text)
    return [int(mid) for mid in matches]


def _parse_topic_ids(value: str):
    topic_ids = set()
    for part in (value or "").split(","):
        p = part.strip()
        if not p:
            continue
        if p.isdigit():
            topic_ids.add(int(p))
    return topic_ids


class TgGroupTopicsMonitor:
    def __init__(
        self,
        api_id: int = None,
        api_hash: str = "",
        session_name: str = "tg_monitor_session",
        group_id: str = "",
        topic_ids=None,
        messages_file: str = None,
        messages_queue: asyncio.Queue = None,
    ):
        self.api_id = api_id or int(os.environ.get("TG_API_ID", "0"))
        self.api_hash = api_hash or os.environ.get("TG_API_HASH", "")
        self.session_name = session_name or os.environ.get("TG_SESSION", "tg_session")
        self.group_id = (group_id or os.environ.get("TG_GROUP_ID", "")).strip()
        env_topic_ids = _parse_topic_ids(os.environ.get("TG_TOPIC_IDS", ""))
        self.topic_ids = set(topic_ids or env_topic_ids)
        self.messages_file = messages_file or DEFAULT_MESSAGES_FILE
        self.messages_queue = messages_queue
        self.client = None
        self.processed_ids = set()
        self.messages_data = {}
        self.allowed_topic_keys = set(self.topic_ids)

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

    def _message_topic_id(self, msg):
        # Для forum сообщений топик обычно лежит в reply_to_top_id.
        if msg.reply_to and getattr(msg.reply_to, "reply_to_top_id", None):
            return int(msg.reply_to.reply_to_top_id)
        # Фолбэк для некоторых версий/типов сообщений.
        if msg.reply_to and getattr(msg.reply_to, "forum_topic", False):
            if getattr(msg.reply_to, "reply_to_msg_id", None):
                return int(msg.reply_to.reply_to_msg_id)
        return None

    def _is_allowed_topic(self, msg):
        if not self.allowed_topic_keys:
            return True
        topic_id = self._message_topic_id(msg)
        if topic_id is None:
            return False
        return topic_id in self.allowed_topic_keys

    def _is_forum_service_reply(self, msg) -> bool:
        """
        В forum-топиках Telegram часто ставит служебный reply на top message.
        Это не пользовательский реплай внутри обсуждения.
        """
        if not msg or not getattr(msg, "reply_to", None):
            return False
        r = msg.reply_to
        is_forum = bool(getattr(r, "forum_topic", False))
        reply_to_msg_id = getattr(r, "reply_to_msg_id", None)
        reply_to_top_id = getattr(r, "reply_to_top_id", None)
        if not is_forum or not reply_to_msg_id:
            return False
        # Встречаются разные варианты служебных reply:
        # 1) reply_to_msg_id == reply_to_top_id
        # 2) reply_to_msg_id может быть topic_id (например 2/5/6)
        if reply_to_top_id and reply_to_msg_id == reply_to_top_id:
            return True
        if reply_to_msg_id in self.topic_ids:
            return True
        return False

    async def _load_topic_key_map(self, group_entity):
        """
        Строит множество допустимых ключей топиков:
        - id топика (из TG_TOPIC_IDS)
        - top_message соответствующего топика (если отличается)
        """
        if not self.topic_ids or GetForumTopicsRequest is None:
            self.allowed_topic_keys = set(self.topic_ids)
            return

        topics = []
        offset_date = None
        offset_id = 0
        offset_topic = 0
        limit = 100

        while True:
            res = await self.client(
                GetForumTopicsRequest(
                    peer=group_entity,
                    offset_date=offset_date,
                    offset_id=offset_id,
                    offset_topic=offset_topic,
                    limit=limit,
                    q="",
                )
            )
            part = getattr(res, "topics", []) or []
            topics.extend(part)
            if not part or len(part) < limit:
                break
            last = part[-1]
            offset_date = getattr(last, "date", None)
            offset_id = getattr(last, "top_message", 0) or 0
            offset_topic = getattr(last, "id", 0) or 0

        allowed = set(self.topic_ids)
        for t in topics:
            tid = getattr(t, "id", None)
            tmsg = getattr(t, "top_message", None)
            if tid in self.topic_ids and tmsg:
                allowed.add(int(tmsg))

        self.allowed_topic_keys = allowed

    async def _resolve_group_entity(self):
        """
        Поддерживает форматы TG_GROUP_ID:
        - 1234567890
        - -1234567890
        - -1001234567890
        - @username
        """
        raw = (self.group_id or "").strip()
        if not raw:
            raise ValueError("TG_GROUP_ID пустой")

        # username / ссылка
        if not raw.lstrip("-").isdigit():
            return await self.client.get_entity(raw)

        val = int(raw)
        abs_val = abs(val)
        candidates = [val]

        # Для channel/megagroup часто нужен peer id вида -100{channel_id}
        if abs_val < 10**12:
            candidates.extend([int(f"-100{abs_val}"), abs_val])

        last_error = None
        for cand in candidates:
            try:
                return await self.client.get_entity(cand)
            except Exception as e:
                last_error = e
                continue

        raise last_error

    async def _find_root_message_id(self, msg):
        visited = set()
        current_id = msg.id

        if msg.reply_to and msg.reply_to.reply_to_msg_id:
            # Для forum-топика игнорируем служебный reply на top message.
            if not self._is_forum_service_reply(msg):
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
                    # Здесь та же логика: не уходим на служебный top message.
                    if self._is_forum_service_reply(current_msg):
                        return current_id
                    current_id = current_msg.reply_to.reply_to_msg_id
                else:
                    return current_id
            except Exception:
                return current_id

    async def _save_message(self, msg, is_edit=False):
        if not self._is_allowed_topic(msg):
            logger.debug(
                "skip by topic filter: msg_id={} topic_key={} allowed={}",
                msg.id,
                self._message_topic_id(msg),
                sorted(self.allowed_topic_keys),
            )
            return

        text = msg.text or "[медиа/без текста]"
        all_link_ids = _extract_all_tme_ids(text)

        if len(all_link_ids) > 1:
            logger.info(f"Обнаружено {len(all_link_ids)} ссылок в сообщении {msg.id}, обрабатываю каждую отдельно")
            for link_id in all_link_ids:
                await self._save_message_for_link_id(msg, text, link_id, is_edit)
            self.processed_ids.add(msg.id)
            return

        if not is_edit and msg.id in self.processed_ids:
            logger.info(f"skip by processed_ids (new): msg.id={msg.id}")
            return
        if is_edit and msg.id not in self.processed_ids:
            logger.info(f"edit без записи в processed_ids — обрабатываем как первое появление: msg.id={msg.id}")
            is_edit = False

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
                        logger.warning(f"Не удалось получить root сообщение {root_id} для сообщения {msg.id}")
                        self.messages_data[root_id] = {root_id: "[root не найден]"}
                        self.processed_ids.add(root_id)
                except Exception as e:
                    logger.warning(f"Ошибка при получении root сообщения {root_id} для сообщения {msg.id}: {e}")
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
        if full_text and full_text.strip() and full_text.strip() != "[медиа/без текста]" and self.messages_queue is not None:
            self.messages_queue.put_nowait({"text": full_text, "root_id": root_id})

        prefix = "[EDIT]" if is_edit else "[NEW]"
        timestamp = msg.date.strftime("%Y-%m-%d %H:%M:%S")
        topic_id = self._message_topic_id(msg)
        topic_info = f" | TOPIC: {topic_id}" if topic_id is not None else ""
        reply_info = f" | ROOT: {root_id}" if root_id != msg.id else ""
        log_payload = full_text if root_id != msg.id and full_text else text
        logger.info(f"{prefix} {timestamp} | ID: {msg.id}{topic_info}{reply_info} | {log_payload}")
        self.processed_ids.add(msg.id)

    async def _save_message_for_link_id(self, msg, text, link_id, is_edit=False):
        root_id = link_id

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

        if isinstance(self.messages_data[root_id], str):
            root_text = self.messages_data[root_id]
            self.messages_data[root_id] = {root_id: root_text}
        if not isinstance(self.messages_data[root_id], dict):
            self.messages_data[root_id] = {root_id: self.messages_data[root_id]}

        if is_edit:
            old_text = self.messages_data[root_id].get(msg.id)
            if old_text is not None and old_text == text:
                return

        self.messages_data[root_id][msg.id] = text
        self._save_messages_data()

        full_text = self._thread_text(root_id)
        if full_text and full_text.strip() and full_text.strip() != "[медиа/без текста]" and self.messages_queue is not None:
            self.messages_queue.put_nowait({"text": full_text, "root_id": root_id})

        prefix = "[EDIT]" if is_edit else "[NEW]"
        timestamp = msg.date.strftime("%Y-%m-%d %H:%M:%S")
        topic_id = self._message_topic_id(msg)
        topic_info = f" | TOPIC: {topic_id}" if topic_id is not None else ""
        log_payload = full_text if full_text else text
        logger.info(f"{prefix} {timestamp} | ID: {msg.id}{topic_info} | LINK: {link_id} | {log_payload}")

    async def run(self):
        if self.messages_queue is None:
            self.messages_queue = asyncio.Queue()

        if not self.group_id:
            logger.error("Не задан TG_GROUP_ID")
            return

        self._load_messages_data()
        self.processed_ids.update(self.messages_data.keys())
        for _, data in self.messages_data.items():
            if isinstance(data, dict):
                self.processed_ids.update(data.keys())

        session_path = os.path.join(SESSIONS_DIR, self.session_name)
        self.client = TelegramClient(session_path, self.api_id, self.api_hash)
        await self.client.start()

        try:
            group_entity = await self._resolve_group_entity()
            await self._load_topic_key_map(group_entity)
            logger.info(
                f"Мониторинг группы: {getattr(group_entity, 'title', self.group_id)} | TG_TOPIC_IDS={sorted(self.topic_ids) if self.topic_ids else 'ALL'} | RESOLVED_TOPIC_KEYS={sorted(self.allowed_topic_keys) if self.allowed_topic_keys else 'ALL'}"
            )
        except Exception as e:
            logger.error(f"Ошибка при получении группы {self.group_id}: {e}")
            return

        @self.client.on(events.NewMessage(chats=group_entity))
        async def on_new_message(event):
            try:
                await self._save_message(event.message, is_edit=False)
            except Exception as e:
                logger.error(f"Ошибка при сохранении сообщения {event.message.id}: {e}")

        @self.client.on(events.MessageEdited(chats=group_entity))
        async def on_edit(event):
            if not event.message.out:
                try:
                    await self._save_message(event.message, is_edit=True)
                except Exception as e:
                    logger.error(f"Ошибка при сохранении редактирования {event.message.id}: {e}")

        logger.info("Хендлеры группы/топиков зарегистрированы, ожидание сообщений...")
        await self.client.run_until_disconnected()


if __name__ == "__main__":
    monitor = TgGroupTopicsMonitor()
    asyncio.run(monitor.run())
