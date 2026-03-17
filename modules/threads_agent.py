"""
Threads Agent — два сценария:
1. NEWS: читает TG-каналы через Telethon, делает рерайт через Claude, отправляет на апрув
2. FORMAT: генерирует посты по расписанию форматов, отправляет на апрув

Запускается через APScheduler:
- 09:00 МСК — сценарий NEWS (слот 1)
- 18:00 МСК — сценарий NEWS (слот 2)
- 08:30 МСК — сценарий FORMAT (генерирует посты на день)
"""

import asyncio
import logging
import os
import random
from datetime import datetime, timezone, timedelta

import aiosqlite
import httpx

from config import (
    DB_PATH, OPENROUTER_API_KEY,
)

logger = logging.getLogger(__name__)

# Расписание форматов по дням (0=пн, 6=вс)
SCHEDULE = {
    0: [("ГОРЯЧИЙ ТЕЙК", 0), ("ОДИН ПРОМПТ", 1)],
    1: [("НОВОСТЬ + УГОЛ", 0), ("ГОРЯЧИЙ ТЕЙК", 1)],
    2: [("ОДИН ПРОМПТ", 0), ("ДО / ПОСЛЕ", 1)],
    3: [("НОВОСТЬ + УГОЛ", 0), ("СПИСОК БЕЗ ВОДЫ", 1)],
    4: [("ГОРЯЧИЙ ТЕЙК", 0), ("ОДИН ПРОМПТ", 1)],
    5: [("СПИСОК БЕЗ ВОДЫ", 0)],
    # 6 = воскресенье — выходной
}

HASHTAG_POOL = [
    "#нейросети", "#ИИ", "#ChatGPT", "#Claude", "#промпты",
    "#нейросеть", "#AI", "#искусственныйинтеллект", "#нейросети2025", "#AIинструменты"
]

SYSTEM_PROMPT_NEWS = """Ты — контент-агент русскоязычного Telegram-канала про нейросети и AI.
Пишешь посты для Threads от лица AI-энтузиаста. Умный, уверенный, без воды.

НЕЛЬЗЯ: вступления типа «Привет», «возможно», «наверное», корпоративный язык, «революционный», «невероятный».
МОЖНО: начинать с сути, цифры, конкретика, провокационные вопросы.

Формат НОВОСТЬ + УГОЛ:
[Факт/новость — 1 предложение, только суть]

[Что это значит для обычного человека — 2-3 предложения]

[Неожиданный угол или вывод — 1-2 предложения]

[2-3 хештега из предоставленных]

Жёсткий лимит: не более 500 символов. Варьируй длину: иногда 150-200, иногда 350-450."""

SYSTEM_PROMPT_FORMAT = """Ты — контент-агент русскоязычного Telegram-канала про нейросети и AI.
Пишешь посты для Threads от лица AI-энтузиаста. Умный, уверенный, без воды.

НЕЛЬЗЯ: вступления типа «Привет», «возможно», «наверное», корпоративный язык, «революционный», «невероятный».
МОЖНО: начинать с сути, цифры, конкретика, провокационные вопросы.

ФОРМАТЫ:

ГОРЯЧИЙ ТЕЙК:
[Спорное утверждение — 1 предложение]

[Объяснение с конкретикой — 2-3 предложения]

[Вопрос или провокация — 1 предложение]

[хештеги]

ОДИН ПРОМПТ:
[Проблема которую решает промпт — 1 предложение]

«[Готовый промпт к копипасту]»

[Что получишь — 1 предложение]

Сохрани.

[хештеги]

ДО / ПОСЛЕ:
[Задача]
Без AI — [результат]
С AI — [результат]

[Конкретный инструмент + как — 2-3 предложения]

[Неожиданный вывод — 1 предложение]

[хештеги]

СПИСОК БЕЗ ВОДЫ:
[Крючок с цифрой — 1 предложение]

— [пункт 1]
— [пункт 2]
— [пункт 3]
— [пункт 4]
— [пункт 5]

[Вопрос или финальный удар]

[хештеги]

Жёсткий лимит: не более 500 символов. Варьируй длину."""


# ─────────────────────────────────────
# Telethon: чтение каналов
# ─────────────────────────────────────

async def _get_telethon_client() -> TelegramClient:
    client = TelegramClient(TELETHON_SESSION, TELETHON_API_ID, TELETHON_API_HASH)
    await client.start()
    return client


async def fetch_channel_posts(channel: str, hours: int = THREADS_LOOKBACK_HOURS) -> list[dict]:
    """Читает посты из канала за последние N часов."""
    posts = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    try:
        client = await _get_telethon_client()
        async with client:
            async for msg in client.iter_messages(channel, limit=50):
                if not msg.date:
                    continue
                msg_date = msg.date.replace(tzinfo=timezone.utc) if msg.date.tzinfo is None else msg.date
                if msg_date < cutoff:
                    break

                text = msg.text or msg.caption or ""
                if not text or len(text) < 50:
                    continue

                post = {
                    "source": channel,
                    "text": text,
                    "media_type": None,
                    "media_path": None,
                    "date": msg_date,
                }

                # Скачиваем медиа если есть
                if msg.media:
                    if isinstance(msg.media, MessageMediaPhoto):
                        post["media_type"] = "photo"
                    elif isinstance(msg.media, MessageMediaDocument):
                        if msg.media.document.mime_type.startswith("video"):
                            post["media_type"] = "video"

                    if post["media_type"]:
                        try:
                            path = await client.download_media(msg.media, file=f"/tmp/threads_media_{msg.id}")
                            post["media_path"] = path
                        except Exception as e:
                            logger.warning(f"Не удалось скачать медиа: {e}")

                posts.append(post)

    except Exception as e:
        logger.error(f"Ошибка чтения канала @{channel}: {e}")

    logger.info(f"Канал @{channel}: найдено {len(posts)} постов за {hours}ч")
    return posts


# ─────────────────────────────────────
# Claude: генерация текста
# ─────────────────────────────────────

async def _call_claude(system: str, user: str) -> str:
    hashtags = " ".join(random.sample(HASHTAG_POOL, random.randint(2, 3)))
    user_with_tags = f"{user}\n\nХЕШТЕГИ (использовать только эти): {hashtags}"

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "anthropic/claude-sonnet-4-6",
                "max_tokens": 600,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_with_tags},
                ],
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()


async def rewrite_for_threads(post: dict) -> str:
    """Рерайтит пост из TG-канала под формат Threads."""
    user_prompt = f"""Перепиши этот пост из Telegram-канала в формат НОВОСТЬ + УГОЛ для Threads.
Источник: @{post['source']}

Исходный текст:
{post['text'][:1500]}"""

    return await _call_claude(SYSTEM_PROMPT_NEWS, user_prompt)


async def generate_format_post(format_name: str) -> str:
    """Генерирует пост по заданному формату."""
    user_prompt = f"""Напиши один пост в формате {format_name}.
Тема: нейросети, AI-инструменты, промпты, продуктивность с AI.
Используй свежие знания об AI-индустрии."""

    return await _call_claude(SYSTEM_PROMPT_FORMAT, user_prompt)


# ─────────────────────────────────────
# Сохранение в БД
# ─────────────────────────────────────

async def save_post(type_: str, source: str, text: str,
                    media_url: str = None, media_type: str = None) -> int:
    """Сохраняет пост в threads_posts, возвращает id."""
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """INSERT INTO threads_posts (type, source, text, media_url, media_type, status, created_at)
               VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
            (type_, source, text, media_url, media_type, now),
        )
        await db.commit()
        return cur.lastrowid


# ─────────────────────────────────────
# Загрузка медиа на catbox.moe
# ─────────────────────────────────────

async def upload_media(file_path: str) -> str | None:
    """Загружает файл на catbox.moe, возвращает публичный URL."""
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            with open(file_path, "rb") as f:
                resp = await client.post(
                    "https://catbox.moe/user/api.php",
                    data={"reqtype": "fileupload"},
                    files={"fileToUpload": f},
                )
            if resp.status_code == 200 and resp.text.startswith("https://"):
                return resp.text.strip()
    except Exception as e:
        logger.warning(f"Ошибка загрузки медиа: {e}")
    return None


# ─────────────────────────────────────
# Основные задачи для APScheduler
# ─────────────────────────────────────

async def run_news_job(bot, admin_chat_id: int):
    """
    Сценарий 1: читает каналы, генерирует рерайт, отправляет на апрув.
    Запускается в 09:00 и 18:00 МСК.
    """
    from modules.threads_poster import send_for_approval

    logger.info("[Threads NEWS] Запуск")
    all_posts = []

    for channel in THREADS_SOURCE_CHANNELS:
        posts = await fetch_channel_posts(channel)
        all_posts.extend(posts)

    if not all_posts:
        logger.info("[Threads NEWS] Новых постов не найдено")
        return

    # Берём 1-2 лучших поста (самые свежие)
    all_posts.sort(key=lambda p: p["date"], reverse=True)
    to_process = all_posts[:2]

    for post in to_process:
        try:
            text = await rewrite_for_threads(post)

            # Загружаем медиа если есть
            media_url = None
            if post.get("media_path"):
                media_url = await upload_media(post["media_path"])
                try:
                    os.remove(post["media_path"])
                except Exception:
                    pass

            post_id = await save_post(
                type_="news",
                source=post["source"],
                text=text,
                media_url=media_url,
                media_type=post.get("media_type"),
            )

            await send_for_approval(bot, admin_chat_id, post_id)
            logger.info(f"[Threads NEWS] Пост {post_id} отправлен на апрув")

        except Exception as e:
            logger.error(f"[Threads NEWS] Ошибка обработки поста: {e}")


async def run_format_job(bot, admin_chat_id: int):
    """
    Сценарий 2: генерирует посты по расписанию форматов, отправляет на апрув.
    Запускается в 08:30 МСК.
    """
    from modules.threads_poster import send_for_approval

    now_msk = datetime.now(timezone.utc) + timedelta(hours=3)
    day = now_msk.weekday()

    if day == 6:
        logger.info("[Threads FORMAT] Воскресенье — выходной")
        return

    formats = SCHEDULE.get(day, [])
    if not formats:
        return

    logger.info(f"[Threads FORMAT] День {day}, форматов: {len(formats)}")

    for format_name, _ in formats:
        try:
            text = await generate_format_post(format_name)
            post_id = await save_post(type_="format", source=format_name, text=text)
            await send_for_approval(bot, admin_chat_id, post_id)
            logger.info(f"[Threads FORMAT] Пост {post_id} ({format_name}) отправлен на апрув")
            await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"[Threads FORMAT] Ошибка генерации {format_name}: {e}")
