import os
import asyncio
import glob

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError


load_dotenv()
from loguru import logger

_BASE_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
SESSIONS_DIR = os.path.join(_BASE_DIR, "tg_sessions")
os.makedirs(SESSIONS_DIR, exist_ok=True)

API_ID = int(os.environ.get("TG_API_ID", "0"))
API_HASH = os.environ.get("TG_API_HASH", "")
SESSION_NAME = os.environ.get("TG_SESSION", "tg_session")


async def main():
    session_path = os.path.join(SESSIONS_DIR, SESSION_NAME)
    
    # Удаляем все файлы сессии (включая .session-journal и другие)
    session_files = glob.glob(f"{session_path}*")
    for f in session_files:
        try:
            os.remove(f)
            logger.info(f"Удален файл сессии: {f}")
        except Exception as e:
            logger.error(f"Не удалось удалить {f}: {e}")
    
    client = TelegramClient(session_path, API_ID, API_HASH)

    await client.start()

    if not await client.is_user_authorized():
        phone = input("Номер в формате +7XXXXXXXXXX (или свой международный): ").strip()
        await client.send_code_request(phone)
        code = input("Код: ").strip()
        try:
            await client.sign_in(phone, code)
        except SessionPasswordNeededError:
            pwd = input("Облачный пароль (2FA): ").strip()
            await client.sign_in(password=pwd)

    logger.info("Успешная авторизация. Сессия сохранена.")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
