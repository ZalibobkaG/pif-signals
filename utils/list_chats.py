import os
import asyncio

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.functions.messages import GetForumTopicsRequest


load_dotenv()

_BASE_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
SESSIONS_DIR = os.path.join(_BASE_DIR, "tg_sessions")
os.makedirs(SESSIONS_DIR, exist_ok=True)

API_ID = int(os.environ.get("TG_API_ID", "0"))
API_HASH = os.environ.get("TG_API_HASH", "")
SESSION_NAME = os.environ.get("TG_SESSION", "tg_session")


async def main():
    session_path = os.path.join(SESSIONS_DIR, SESSION_NAME)
    client = TelegramClient(session_path, API_ID, API_HASH)

    await client.start()

    print("\n" + "=" * 80)
    print("СПИСОК КАНАЛОВ И ГРУПП")
    print("=" * 80 + "\n")

    channels = []
    groups = []

    async def fetch_forum_topics(channel_entity):
        """
        Возвращает список топиков forum-группы:
        [{'id': int, 'title': str}, ...]
        """
        topics = []
        offset_date = None
        offset_id = 0
        offset_topic = 0
        limit = 100

        while True:
            # В разных версиях Telethon сигнатура отличается: peer=... или channel=...
            try:
                req = GetForumTopicsRequest(
                    peer=channel_entity,
                    offset_date=offset_date,
                    offset_id=offset_id,
                    offset_topic=offset_topic,
                    limit=limit,
                    q="",
                )
            except TypeError:
                req = GetForumTopicsRequest(
                    channel=channel_entity,
                    offset_date=offset_date,
                    offset_id=offset_id,
                    offset_topic=offset_topic,
                    limit=limit,
                    q="",
                )

            res = await client(req)

            part = getattr(res, "topics", []) or []
            for t in part:
                topics.append(
                    {
                        "id": getattr(t, "id", None),
                        "title": getattr(t, "title", "") or "<без названия>",
                    }
                )

            # Пагинация: берём "хвост" и продолжаем, пока есть чем листать.
            if not part or len(part) < limit:
                break

            last = part[-1]
            offset_date = getattr(last, "date", 0) or 0
            offset_id = getattr(last, "top_message", 0) or 0
            offset_topic = getattr(last, "id", 0) or 0

        return topics

    from telethon.tl.types import Channel
    
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        
        if isinstance(entity, Channel) and entity.broadcast:  # Каналы
            channels.append({
                'title': entity.title,
                'id': entity.id,
                'username': getattr(entity, 'username', None),
            })
        elif isinstance(entity, Channel) and entity.megagroup:  # Супергруппы (включая forum)
            groups.append({
                'title': entity.title,
                'id': entity.id,
                'username': getattr(entity, 'username', None),
                'is_forum': bool(getattr(entity, 'forum', False)),
            })

    if channels:
        print("📢 КАНАЛЫ:")
        print("-" * 80)
        for ch in sorted(channels, key=lambda x: x['title']):
            username_str = f" @{ch['username']}" if ch['username'] else ""
            print(f"  {ch['title']}{username_str}")
            print(f"    ID: {ch['id']}")
            print()
    else:
        print("Каналы не найдены")

    if groups:
        print("👥 ГРУППЫ / СУПЕРГРУППЫ:")
        print("-" * 80)
        for grp in sorted(groups, key=lambda x: x['title']):
            username_str = f" @{grp['username']}" if grp['username'] else ""
            forum_str = " [forum topics enabled]" if grp['is_forum'] else ""
            print(f"  {grp['title']}{username_str}{forum_str}")
            print(f"    ID: {grp['id']}")
            if grp["is_forum"]:
                try:
                    forum_entity = await client.get_entity(grp["id"])
                    topics = await fetch_forum_topics(forum_entity)
                    if topics:
                        print("    Topics:")
                        for topic in topics:
                            print(f"      - {topic['title']} (topic_id={topic['id']})")
                    else:
                        print("    Topics: не найдены")
                except Exception as e:
                    print(f"    Topics: ошибка чтения ({e})")
            print()
    else:
        print("Группы не найдены")

    print("=" * 80)
    print("\nДля использования в .env укажите:")
    print("TG_CHANNEL=<ID_канала_или_группы>")
    print("TG_TOPIC_ID=<ID_топика_для_forum_группы>")
    print("\nИли используйте username (если есть):")
    print(f"TG_CHANNEL=@username")
    print("=" * 80)

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
