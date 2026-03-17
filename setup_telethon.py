"""
Одноразовый скрипт для авторизации Telethon.
Запускать на сервере ОДИН РАЗ вручную:

    cd ~/Autopasting-Syntx
    python setup_telethon.py

После успешной авторизации появится файл telethon_session.session
Больше этот скрипт запускать не нужно — сессия сохранена.
"""

import asyncio
from dotenv import load_dotenv
from config import TELETHON_API_ID, TELETHON_API_HASH, TELETHON_PHONE, TELETHON_SESSION
from telethon import TelegramClient

load_dotenv()


async def main():
    print(f"Подключаюсь с API_ID={TELETHON_API_ID}...")
    client = TelegramClient(TELETHON_SESSION, TELETHON_API_ID, TELETHON_API_HASH)

    await client.start(phone=TELETHON_PHONE)

    me = await client.get_me()
    print(f"\nУспешно! Авторизован как: {me.first_name} (@{me.username})")
    print(f"Файл сессии: {TELETHON_SESSION}.session")
    print("\nТеперь можно запускать основной бот.")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
