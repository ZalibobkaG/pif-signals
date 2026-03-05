import os
import asyncio

from dotenv import load_dotenv
from telethon import TelegramClient

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
    print("СПИСОК КАНАЛОВ")
    print("=" * 80 + "\n")

    channels = []

    from telethon.tl.types import Channel
    
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        
        if isinstance(entity, Channel) and entity.broadcast:  # Только каналы
            channels.append({
                'title': entity.title,
                'id': entity.id,
                'username': getattr(entity, 'username', None),
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

    print("=" * 80)
    print("\nДля использования в .env укажите:")
    print(f"TG_CHANNEL=<ID_канала>")
    print("\nИли используйте username (если есть):")
    print(f"TG_CHANNEL=@username")
    print("=" * 80)

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
