import asyncio
import logging
import math
import random
from datetime import date, datetime, timedelta

import aiosqlite
import pytz

from config import (
    DB_PATH,
    DELAY_MAKE_WEBHOOK,
    IMAGES_PER_DAY_MAX,
    IMAGES_PER_DAY_MIN,
    PINTEREST_FILE_TTL_DAYS,
    TIMEZONE,
)
from database import get_state, set_state
from modules import drive, publisher, sheets

logger = logging.getLogger(__name__)
tz = pytz.timezone(TIMEZONE)


def _distribute_pins(total: int, days: int, min_per_day: int, max_per_day: int) -> list[int]:
    """Distribute pins roughly evenly across days within min/max bounds."""
    base = total // days
    base = max(min_per_day, min(max_per_day, base))
    result = [base] * days
    remainder = total - sum(result)
    for i in range(abs(remainder)):
        if remainder > 0 and result[i % days] < max_per_day:
            result[i % days] += 1
        elif remainder < 0 and result[i % days] > min_per_day:
            result[i % days] -= 1
    return result


async def setup_posting_schedule(bot, chat_id: int):
    try:
        # Load sheets for category data
        await sheets.load_sheets()
        sheets_data = sheets.get_cached()

        # Get pending generations with their pinterest file IDs
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT g.id, g.pinterest_file_id, r.category
                   FROM generations g
                   JOIN refs r ON g.reference_id = r.id
                   WHERE g.status = 'success' AND g.pinterest_file_id IS NOT NULL
                     AND g.id NOT IN (SELECT generation_id FROM pins_schedule)"""
            ) as cur:
                ready = await cur.fetchall()

        if not ready:
            await bot.send_message(chat_id, "Нет готовых изображений для постинга.")
            return

        total = len(ready)
        days = math.ceil(total / IMAGES_PER_DAY_MAX)
        days = max(days, math.ceil(total / IMAGES_PER_DAY_MIN))

        distribution = _distribute_pins(total, days, IMAGES_PER_DAY_MIN, IMAGES_PER_DAY_MAX)

        # Interleave categories
        by_category: dict[str, list] = {}
        for row in ready:
            by_category.setdefault(row["category"], []).append(dict(row))

        ordered = []
        cats = list(by_category.keys())
        max_len = max(len(v) for v in by_category.values())
        for i in range(max_len):
            for cat in cats:
                if i < len(by_category[cat]):
                    ordered.append(by_category[cat][i])

        # Assign scheduled times
        start_date = date.today() + timedelta(days=1)
        posting_hours = list(range(9, 22))  # 9:00-21:00 МСК
        schedule_entries = []

        idx = 0
        for day_offset, count in enumerate(distribution):
            day = start_date + timedelta(days=day_offset)
            times = sorted(random.sample(posting_hours, min(count, len(posting_hours))))
            for hour in times:
                if idx >= len(ordered):
                    break
                item = ordered[idx]
                dt = tz.localize(datetime(day.year, day.month, day.day, hour, random.randint(0, 59)))
                cat_data = sheets_data.get(item["category"], {})
                board_id = cat_data.get("board_id", "")
                schedule_entries.append({
                    "generation_id": item["id"],
                    "gdrive_file_id": item["pinterest_file_id"],
                    "category": item["category"],
                    "board_id": board_id,
                    "scheduled_at": dt.isoformat(),
                })
                idx += 1

        # Write to DB
        async with aiosqlite.connect(DB_PATH) as db:
            for entry in schedule_entries:
                await db.execute(
                    """INSERT INTO pins_schedule
                       (generation_id, gdrive_file_id, category, board_id, scheduled_at, status)
                       VALUES (?, ?, ?, ?, ?, 'pending')""",
                    (
                        entry["generation_id"],
                        entry["gdrive_file_id"],
                        entry["category"],
                        entry["board_id"],
                        entry["scheduled_at"],
                    ),
                )
            await db.commit()

        end_date = start_date + timedelta(days=days - 1)
        await set_state(
            posting_status="running",
            posting_start_date=start_date.isoformat(),
            posting_end_date=end_date.isoformat(),
        )

        await bot.send_message(
            chat_id,
            f"{total} пинов на {days} дней ({IMAGES_PER_DAY_MIN}-{IMAGES_PER_DAY_MAX}/день).\n"
            f"Начало: {start_date.strftime('%d.%m.%Y')}, конец: {end_date.strftime('%d.%m.%Y')} (МСК)\n\n"
            f"Постинг запущен автоматически по расписанию."
        )

    except Exception as e:
        logger.error(f"Schedule setup failed: {e}")
        await bot.send_message(chat_id, f"Ошибка планирования: {e}")
        await set_state(posting_status="idle")


async def publish_due_pins(bot, admin_chat_id: int):
    """Called by APScheduler every minute. Publishes pins whose scheduled_at has passed."""
    now = datetime.now(tz).isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM pins_schedule WHERE status = 'pending' AND scheduled_at <= ?",
            (now,),
        ) as cur:
            due = await cur.fetchall()

    for row in due:
        ok = await publisher.publish_pin(
            pin_id=row["id"],
            file_id=row["gdrive_file_id"],
            category=row["category"],
            board_id=row["board_id"],
        )
        if not ok:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE pins_schedule SET status = 'failed' WHERE id = ?", (row["id"],)
                )
                await db.commit()
        await asyncio.sleep(DELAY_MAKE_WEBHOOK)

    await _check_posting_completion(bot, admin_chat_id)


async def _check_posting_completion(bot, chat_id: int):
    state = await get_state()
    if state.get("posting_status") != "running":
        return

    end_date_str = state.get("posting_end_date")
    if not end_date_str:
        return

    end_date = date.fromisoformat(end_date_str)
    today = date.today()
    days_left = (end_date - today).days

    if days_left == 2:
        await bot.send_message(chat_id, "Через 2 дня постинг завершается. Подготовь новые референсы.")
    elif days_left < 0:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT COUNT(*) as cnt FROM pins_schedule WHERE status = 'pending'"
            ) as cur:
                row = await cur.fetchone()
        if row and row["cnt"] == 0:
            await set_state(posting_status="done")
            await bot.send_message(
                chat_id,
                "Постинг завершён! Все пины опубликованы.\n\n"
                "Запустить новый цикл? → /pinterest_analyze"
            )


async def cleanup_old_pinterest_files():
    """Called by APScheduler daily. Deletes pinterest/ Drive files older than TTL."""
    from datetime import timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(days=PINTEREST_FILE_TTL_DAYS)).isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, gdrive_file_id FROM pins_schedule "
            "WHERE status = 'published' AND published_at <= ? AND gdrive_file_id IS NOT NULL",
            (cutoff,),
        ) as cur:
            to_clean = await cur.fetchall()

    for row in to_clean:
        try:
            await drive.delete_file(row["gdrive_file_id"])
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE pins_schedule SET status = 'cleaned', gdrive_file_id = NULL WHERE id = ?",
                    (row["id"],),
                )
                await db.commit()
            logger.info(f"Cleaned pin {row['id']} file {row['gdrive_file_id']}")
        except Exception as e:
            logger.error(f"Cleanup error for pin {row['id']}: {e}")
