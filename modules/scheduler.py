import asyncio
import logging
import math
import random
from datetime import date, datetime, timedelta

import aiosqlite
import pytz

# Set by main.py before scheduler starts
_bot = None
_admin_chat_id = None


async def publish_due_pins_job():
    """Wrapper without args — APScheduler calls this every minute."""
    if _bot and _admin_chat_id:
        await publish_due_pins(_bot, _admin_chat_id)


async def publish_due_tg_posts_job():
    """Wrapper without args — APScheduler calls this every minute."""
    if _bot:
        await publish_due_tg_posts(_bot)


from config import (
    DB_PATH,
    DELAY_MAKE_WEBHOOK,
    IMAGES_PER_DAY_MAX,
    IMAGES_PER_DAY_MIN,
    PINTEREST_FILE_TTL_DAYS,
    TG_POST_HOUR_START,
    TG_POST_HOUR_END,
    TIMEZONE,
)
from database import get_state, set_state
from modules import drive, publisher, sheets

logger = logging.getLogger(__name__)
tz = pytz.timezone(TIMEZONE)


def _distribute_pins(total: int, days: int, min_per_day: int, max_per_day: int) -> list[int]:
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


def _in_tg_window(hour: int) -> bool:
    """True if hour is within [TG_POST_HOUR_START, TG_POST_HOUR_END) wrapping midnight."""
    if TG_POST_HOUR_START <= TG_POST_HOUR_END:
        return TG_POST_HOUR_START <= hour < TG_POST_HOUR_END
    else:  # wraps midnight (e.g. 10–01)
        return hour >= TG_POST_HOUR_START or hour < TG_POST_HOUR_END


def _next_tg_slot(now: datetime) -> datetime:
    """Return next available TG posting time within window (wraps midnight)."""
    now_local = now.astimezone(tz)
    if _in_tg_window(now_local.hour):
        return now_local + timedelta(minutes=random.randint(1, 5))
    # Outside window — next window start today or tomorrow
    today_start = now_local.replace(
        hour=TG_POST_HOUR_START, minute=random.randint(0, 30), second=0, microsecond=0
    )
    if today_start > now_local:
        return today_start
    return today_start + timedelta(days=1)


async def setup_posting_schedule(bot, chat_id: int):
    try:
        await sheets.load_sheets()
        sheets_data = sheets.get_cached()

        # Get pin-type files not yet scheduled
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT gf.id, gf.ref_id, gf.gdrive_file_id, gf.model, r.category
                   FROM generation_files gf
                   JOIN refs r ON gf.ref_id = r.id
                   WHERE gf.type = 'pin'
                     AND gf.id NOT IN (
                         SELECT generation_file_id FROM pins_schedule
                         WHERE generation_file_id IS NOT NULL
                     )
                   ORDER BY gf.ref_id, gf.model, gf.id"""
            ) as cur:
                rows = await cur.fetchall()

        if not rows:
            await bot.send_message(chat_id, "Нет готовых изображений для постинга.")
            return

        total = len(rows)
        days = math.ceil(total / IMAGES_PER_DAY_MAX)
        days = max(days, math.ceil(total / IMAGES_PER_DAY_MIN))
        distribution = _distribute_pins(total, days, IMAGES_PER_DAY_MIN, IMAGES_PER_DAY_MAX)

        # Determine start date
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT MAX(scheduled_at) as last FROM pins_schedule WHERE status = 'pending'"
            ) as cur:
                row = await cur.fetchone()
                last_scheduled = row[0] if row and row[0] else None

        if last_scheduled:
            last_date = date.fromisoformat(last_scheduled[:10])
            start_date = max(last_date, date.today() - timedelta(days=1)) + timedelta(days=1)
        else:
            start_date = date.today()

        posting_hours = list(range(9, 24)) + [0]  # 09:00–00:59 MSK
        schedule_entries = []
        idx = 0

        for day_offset, count in enumerate(distribution):
            day = start_date + timedelta(days=day_offset)
            times = sorted(random.sample(posting_hours, min(count, len(posting_hours))))
            for hour in times:
                if idx >= len(rows):
                    break
                item = rows[idx]
                dt = tz.localize(datetime(day.year, day.month, day.day, hour, random.randint(0, 59)))
                cat_data = sheets_data.get(item["category"], {})
                board_id = cat_data.get("board_id", "")
                schedule_entries.append({
                    "generation_file_id": item["id"],
                    "ref_id": item["ref_id"],
                    "gdrive_file_id": item["gdrive_file_id"],
                    "category": item["category"],
                    "board_id": board_id,
                    "scheduled_at": dt.isoformat(),
                })
                idx += 1

        async with aiosqlite.connect(DB_PATH) as db:
            for entry in schedule_entries:
                await db.execute(
                    """INSERT INTO pins_schedule
                       (generation_file_id, ref_id, gdrive_file_id, category, board_id, scheduled_at, status)
                       VALUES (?, ?, ?, ?, ?, ?, 'pending')""",
                    (
                        entry["generation_file_id"],
                        entry["ref_id"],
                        entry["gdrive_file_id"],
                        entry["category"],
                        entry["board_id"],
                        entry["scheduled_at"],
                    ),
                )
            await db.commit()

        end_date = start_date + timedelta(days=days - 1)
        state = await get_state()
        existing_start = state.get("posting_start_date")
        await set_state(
            posting_status="running",
            posting_start_date=existing_start or start_date.isoformat(),
            posting_end_date=end_date.isoformat(),
        )

        await bot.send_message(
            chat_id,
            f"Добавлено {total} пинов на {days} дней ({IMAGES_PER_DAY_MIN}-{IMAGES_PER_DAY_MAX}/день).\n"
            f"Постятся с {start_date.strftime('%d.%m.%Y')} по {end_date.strftime('%d.%m.%Y')} (МСК)"
        )

    except Exception as e:
        logger.error(f"Schedule setup failed: {e}")
        await bot.send_message(chat_id, f"Ошибка планирования: {e}")
        await set_state(posting_status="idle")


async def setup_test_schedule(bot, chat_id: int):
    """Schedule all unscheduled pins immediately (for testing all categories)."""
    try:
        await sheets.load_sheets()
        sheets_data = sheets.get_cached()

        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT gf.id, gf.ref_id, gf.gdrive_file_id, gf.model, r.category
                   FROM generation_files gf
                   JOIN refs r ON gf.ref_id = r.id
                   WHERE gf.type = 'pin'
                     AND gf.id NOT IN (
                         SELECT generation_file_id FROM pins_schedule
                         WHERE generation_file_id IS NOT NULL
                     )
                   ORDER BY gf.ref_id, gf.model, gf.id"""
            ) as cur:
                rows = await cur.fetchall()

        if not rows:
            await bot.send_message(chat_id, "Нет готовых изображений для постинга.")
            return

        now = datetime.now(tz)
        async with aiosqlite.connect(DB_PATH) as db:
            for i, item in enumerate(rows):
                dt = now + timedelta(seconds=i * 5)
                cat_data = sheets_data.get(item["category"]) or sheets_data.get(f"ПРОМПТЫ / {item['category']}") or {}
                board_id = cat_data.get("board_id", "")
                await db.execute(
                    """INSERT INTO pins_schedule
                       (generation_file_id, ref_id, gdrive_file_id, category, board_id, scheduled_at, status)
                       VALUES (?, ?, ?, ?, ?, ?, 'pending')""",
                    (item["id"], item["ref_id"], item["gdrive_file_id"],
                     item["category"], board_id, dt.isoformat()),
                )
            await db.commit()

        await set_state(
            posting_status="running",
            posting_start_date=now.date().isoformat(),
            posting_end_date=now.date().isoformat(),
        )
        await bot.send_message(
            chat_id,
            f"Тест: {len(rows)} пинов запланировано немедленно. Публикация начнётся в течение минуты."
        )

    except Exception as e:
        logger.error(f"Test schedule failed: {e}")
        await bot.send_message(chat_id, f"Ошибка: {e}")
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
        if ok:
            # Check if all pins for this ref are now published → trigger TG post
            ref_id = row["ref_id"]
            if ref_id:
                await _check_ref_tg_trigger(ref_id)
        else:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE pins_schedule SET status = 'failed' WHERE id = ?", (row["id"],)
                )
                await db.commit()
        await asyncio.sleep(DELAY_MAKE_WEBHOOK)

    await _check_posting_completion(bot, admin_chat_id)


async def _check_ref_tg_trigger(ref_id: int):
    """If all pins for ref_id are published, schedule a TG post."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Count total, published and failed pins for this ref
        async with db.execute(
            "SELECT COUNT(*) as total FROM pins_schedule WHERE ref_id = ?", (ref_id,)
        ) as cur:
            total_row = await cur.fetchone()
        async with db.execute(
            "SELECT "
            "SUM(CASE WHEN status='published' THEN 1 ELSE 0 END) as done, "
            "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed "
            "FROM pins_schedule WHERE ref_id = ?",
            (ref_id,),
        ) as cur:
            done_row = await cur.fetchone()

        total = total_row["total"] if total_row else 0
        done = done_row["done"] or 0 if done_row else 0
        failed = done_row["failed"] or 0 if done_row else 0

        if total == 0 or done + failed < total:
            return

        # Check if TG post already pending for this ref (posted = old cycle, allow new one)
        async with db.execute(
            "SELECT id FROM tg_posts WHERE ref_id = ? AND status = 'pending'", (ref_id,)
        ) as cur:
            existing = await cur.fetchone()
        if existing:
            return

        # Schedule TG post
        now = datetime.now(tz)
        scheduled_at = _next_tg_slot(now)
        await db.execute(
            "INSERT INTO tg_posts (ref_id, status, scheduled_at) VALUES (?, 'pending', ?)",
            (ref_id, scheduled_at.isoformat()),
        )
        await db.commit()
        logger.info(f"TG post scheduled for ref_id={ref_id} at {scheduled_at.isoformat()}")


async def publish_due_tg_posts(bot):
    """Called by APScheduler every minute. Posts to TG channel when scheduled time comes."""
    from modules.tg_poster import post_tg

    now = datetime.now(tz).isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT tp.id, tp.ref_id, r.prompts, r.category "
            "FROM tg_posts tp JOIN refs r ON tp.ref_id = r.id "
            "WHERE tp.status = 'pending' AND tp.scheduled_at <= ?",
            (now,),
        ) as cur:
            due = await cur.fetchall()

    for row in due:
        try:
            prompts = json_loads_safe(row["prompts"])
            prompt_text = prompts[0].get("full", "") if prompts else ""
            await post_tg(bot, tg_post_id=row["id"], ref_id=row["ref_id"],
                          prompt=prompt_text, category=row["category"])
        except Exception as e:
            logger.error(f"TG post failed for tg_post_id={row['id']}: {e}")


def json_loads_safe(s):
    import json
    try:
        return json.loads(s)
    except Exception:
        return []


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
                "SELECT COUNT(*) as total, "
                "SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) as pending "
                "FROM pins_schedule"
            ) as cur:
                row = await cur.fetchone()
        total_pins = row["total"] or 0 if row else 0
        pending_pins = row["pending"] or 0 if row else 0
        if total_pins > 0 and pending_pins == 0:
            await set_state(posting_status="done")
            await bot.send_message(
                chat_id,
                "Постинг завершён! Все пины опубликованы.\n\n"
                "Запустить новый цикл? → /pinterest_analyze"
            )


async def cleanup_old_pinterest_files():
    """Called by APScheduler daily. Deletes pin Drive files older than TTL."""
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
