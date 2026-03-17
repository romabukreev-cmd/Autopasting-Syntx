"""
Threads Poster — публикация в Threads API + апрув-флоу через Telegram-бот.

Апрув-флоу:
1. Агент генерирует пост → save_post() → send_for_approval()
2. Бот присылает пост с кнопками: ✅ Опубликовать / ✏️ Редактировать / 🕐 Позже / ❌ Отменить
3. Пользователь выбирает действие
4. При публикации → publish_to_threads()
5. При «Позже» → спрашиваем время → APScheduler ставит в очередь
"""

import logging
import time
from datetime import datetime, timezone

import aiosqlite
import httpx
from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from config import DB_PATH, THREADS_USER_ID, THREADS_ACCESS_TOKEN, THREADS_API_BASE

logger = logging.getLogger(__name__)

# Состояние редактирования: {user_id: post_id}
_WAITING_EDIT: dict[int, int] = {}

# Состояние выбора времени: {user_id: post_id}
_WAITING_TIME: dict[int, int] = {}


# ─────────────────────────────────────
# Threads API
# ─────────────────────────────────────

async def publish_to_threads(text: str, media_url: str = None, media_type: str = None) -> str:
    """
    Публикует пост в Threads. Возвращает ID поста.
    Двухшаговый процесс: создать контейнер → опубликовать.
    """
    async with httpx.AsyncClient(timeout=30) as client:
        # Шаг 1: создаём контейнер
        params = {
            "media_type": "TEXT",
            "text": text,
            "access_token": THREADS_ACCESS_TOKEN,
        }

        if media_url and media_type == "photo":
            params["media_type"] = "IMAGE"
            params["image_url"] = media_url
        elif media_url and media_type == "video":
            params["media_type"] = "VIDEO"
            params["video_url"] = media_url

        resp = await client.post(
            f"{THREADS_API_BASE}/{THREADS_USER_ID}/threads",
            params=params,
        )
        resp.raise_for_status()
        container_id = resp.json()["id"]
        logger.info(f"[Threads] Контейнер создан: {container_id}")

    # Шаг 2: ждём 35 секунд (требование API)
    time.sleep(35)

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{THREADS_API_BASE}/{THREADS_USER_ID}/threads_publish",
            params={
                "creation_id": container_id,
                "access_token": THREADS_ACCESS_TOKEN,
            },
        )
        resp.raise_for_status()
        post_id = resp.json()["id"]
        logger.info(f"[Threads] Опубликовано: {post_id}")
        return post_id


# ─────────────────────────────────────
# Клавиатуры апрува
# ─────────────────────────────────────

def kb_approval(post_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Опубликовать", callback_data=f"th:approve:{post_id}"),
            InlineKeyboardButton(text="❌ Отменить", callback_data=f"th:cancel:{post_id}"),
        ],
        [
            InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"th:edit:{post_id}"),
            InlineKeyboardButton(text="🕐 Позже", callback_data=f"th:schedule:{post_id}"),
        ],
    ])


def kb_time_picker(post_id: int) -> InlineKeyboardMarkup:
    """Кнопки быстрого выбора времени публикации."""
    now = datetime.now(timezone.utc)
    from datetime import timedelta
    buttons = []
    for h in [9, 12, 15, 19, 21]:
        label = f"{h:02d}:00"
        buttons.append(InlineKeyboardButton(
            text=label, callback_data=f"th:time:{post_id}:{h}:00"
        ))
    row1 = buttons[:3]
    row2 = buttons[3:]
    return InlineKeyboardMarkup(inline_keyboard=[
        row1,
        row2,
        [InlineKeyboardButton(text="✍️ Своё время (ЧЧ:ММ)", callback_data=f"th:time_custom:{post_id}")],
        [InlineKeyboardButton(text="← Назад", callback_data=f"th:back:{post_id}")],
    ])


# ─────────────────────────────────────
# Отправка на апрув
# ─────────────────────────────────────

async def send_for_approval(bot: Bot, chat_id: int, post_id: int):
    """Отправляет пост в Telegram для апрува."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM threads_posts WHERE id = ?", (post_id,)
        ) as cur:
            post = await cur.fetchone()

    if not post:
        logger.error(f"Пост {post_id} не найден")
        return

    source_label = f"@{post['source']}" if post['type'] == 'news' else post['source']
    header = f"📱 Threads | {source_label}\n{'─' * 30}\n"
    media_note = f"\n🖼 Медиа: {post['media_type']}" if post['media_url'] else ""

    msg = await bot.send_message(
        chat_id,
        f"{header}{post['text']}{media_note}",
        reply_markup=kb_approval(post_id),
    )

    # Сохраняем ID сообщения для последующего редактирования
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE threads_posts SET status = 'sent', tg_msg_id = ? WHERE id = ?",
            (msg.message_id, post_id),
        )
        await db.commit()


# ─────────────────────────────────────
# Действия апрува
# ─────────────────────────────────────

async def approve_post(bot: Bot, post_id: int) -> bool:
    """Публикует пост в Threads и обновляет статус."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM threads_posts WHERE id = ? AND status IN ('sent', 'pending')", (post_id,)
        ) as cur:
            post = await cur.fetchone()

    if not post:
        return False

    try:
        await publish_to_threads(
            text=post["text"],
            media_url=post["media_url"],
            media_type=post["media_type"],
        )
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE threads_posts SET status = 'published', published_at = ? WHERE id = ?",
                (now, post_id),
            )
            await db.commit()
        return True
    except Exception as e:
        logger.error(f"Ошибка публикации поста {post_id}: {e}")
        raise


async def cancel_post(post_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE threads_posts SET status = 'cancelled' WHERE id = ?", (post_id,)
        )
        await db.commit()


async def schedule_post(post_id: int, scheduled_at: datetime):
    """Ставит пост в расписание."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE threads_posts SET status = 'scheduled', scheduled_at = ? WHERE id = ?",
            (scheduled_at.isoformat(), post_id),
        )
        await db.commit()


def start_edit(user_id: int, post_id: int) -> str:
    """Переводит пользователя в режим редактирования. Возвращает текущий текст."""
    _WAITING_EDIT[user_id] = post_id
    return ""  # текст подтянется в боте из БД


async def get_post_text(post_id: int) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT text FROM threads_posts WHERE id = ?", (post_id,)
        ) as cur:
            row = await cur.fetchone()
    return row["text"] if row else ""


async def apply_edit(bot: Bot, user_id: int, chat_id: int, new_text: str):
    """Сохраняет отредактированный текст и отправляет на апрув заново."""
    post_id = _WAITING_EDIT.pop(user_id, None)
    if not post_id:
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE threads_posts SET text = ?, status = 'pending' WHERE id = ?",
            (new_text, post_id),
        )
        await db.commit()

    await bot.send_message(chat_id, "Текст обновлён. Отправляю на апрув заново...")
    await send_for_approval(bot, chat_id, post_id)


# ─────────────────────────────────────
# Планировщик: публикация по расписанию
# ─────────────────────────────────────

async def publish_scheduled_threads_job():
    """
    Проверяет threads_posts со статусом 'scheduled' и публикует просроченные.
    Запускается каждую минуту через APScheduler.
    """
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM threads_posts WHERE status = 'scheduled' AND scheduled_at <= ?", (now,)
        ) as cur:
            due = await cur.fetchall()

    for post in due:
        try:
            await publish_to_threads(
                text=post["text"],
                media_url=post["media_url"],
                media_type=post["media_type"],
            )
            published_at = datetime.now(timezone.utc).isoformat()
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE threads_posts SET status = 'published', published_at = ? WHERE id = ?",
                    (published_at, post["id"]),
                )
                await db.commit()
            logger.info(f"[Threads] Запланированный пост {post['id']} опубликован")
        except Exception as e:
            logger.error(f"[Threads] Ошибка публикации запланированного поста {post['id']}: {e}")
