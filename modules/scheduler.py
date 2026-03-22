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
    DAILY_PIN_HARD_LIMIT,
    DB_PATH,
    DELAY_MAKE_WEBHOOK,
    IMAGES_PER_DAY_MAX,
    IMAGES_PER_DAY_MIN,
    PINTEREST_BOARD_ID,
    PINTEREST_FILE_TTL_DAYS,
    TG_POST_HOUR_START,
    TG_POST_HOUR_END,
    TIMEZONE,
)
from database import get_state, set_state
from modules import drive, publisher, sheets

logger = logging.getLogger(__name__)
tz = pytz.timezone(TIMEZONE)
PIN_POST_HOUR_START = 10
PIN_POST_HOUR_END_EXCLUSIVE = 24


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


def _in_pin_window(dt_local: datetime) -> bool:
    return PIN_POST_HOUR_START <= dt_local.hour < PIN_POST_HOUR_END_EXCLUSIVE


def _pin_window_start(dt_local: datetime) -> datetime:
    return dt_local.replace(hour=PIN_POST_HOUR_START, minute=0, second=0, microsecond=0)


def _pin_window_end(dt_local: datetime) -> datetime:
    return dt_local.replace(hour=23, minute=59, second=59, microsecond=0)


def _local_day_utc_bounds(dt_local: datetime) -> tuple[str, str]:
    day_start_local = dt_local.replace(hour=0, minute=0, second=0, microsecond=0)
    next_day_start_local = day_start_local + timedelta(days=1)
    return day_start_local.astimezone(pytz.UTC).isoformat(), next_day_start_local.astimezone(pytz.UTC).isoformat()


async def _count_published_today(db: aiosqlite.Connection, now_local: datetime) -> int:
    # published_at is stored in UTC in publisher.py; count by local-day bounds converted to UTC
    day_start_utc, next_day_start_utc = _local_day_utc_bounds(now_local)
    async with db.execute(
        "SELECT COUNT(*) FROM pins_schedule "
        "WHERE status = 'published' AND published_at >= ? AND published_at < ?",
        (day_start_utc, next_day_start_utc),
    ) as cur:
        return (await cur.fetchone())[0]


async def _defer_due_pins_to_next_window(now_local: datetime) -> int:
    """If we're outside pin window, move already-due pending pins into next 10:00-23:59 window."""
    now_iso = now_local.isoformat()
    if now_local.hour < PIN_POST_HOUR_START:
        window_start = _pin_window_start(now_local)
    else:
        tomorrow = now_local + timedelta(days=1)
        window_start = _pin_window_start(tomorrow)
    window_end = _pin_window_end(window_start)

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id FROM pins_schedule WHERE status = 'pending' AND scheduled_at <= ? ORDER BY scheduled_at ASC",
            (now_iso,),
        ) as cur:
            due_ids = [row[0] for row in await cur.fetchall()]

        if not due_ids:
            return 0

        remaining_seconds = max(0, (window_end - window_start).total_seconds())
        interval = max(30 * 60, remaining_seconds / max(len(due_ids), 1))
        for i, pin_id in enumerate(due_ids):
            slot_dt = window_start + timedelta(seconds=i * interval)
            await db.execute(
                "UPDATE pins_schedule SET scheduled_at = ? WHERE id = ?",
                (slot_dt.isoformat(), pin_id),
            )
        await db.commit()
        logger.warning(
            f"Deferred {len(due_ids)} due pins to next window starting {window_start.isoformat()} "
            f"(outside {PIN_POST_HOUR_START}:00-23:59)."
        )
        return len(due_ids)


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

        now_local = datetime.now(tz)
        # Working window: 10:00-23:59 MSK
        posting_hours = list(range(10, 24))
        schedule_entries = []
        idx = 0
        day_offset = 0
        carry = 0

        while idx < len(rows):
            planned = distribution[day_offset] if day_offset < len(distribution) else IMAGES_PER_DAY_MAX
            count = min(planned + carry, len(rows) - idx)
            day = start_date + timedelta(days=day_offset)
            available_hours = posting_hours[:]

            # If scheduling starts today, skip hours already passed to avoid instant backfill.
            if day == now_local.date():
                available_hours = [h for h in available_hours if h > now_local.hour]

            slots_count = min(count, len(available_hours))
            carry = count - slots_count

            if slots_count > 0:
                times = sorted(random.sample(available_hours, slots_count))
                for hour in times:
                    item = rows[idx]
                    dt = tz.localize(datetime(day.year, day.month, day.day, hour, random.randint(0, 59)))
                    schedule_entries.append({
                        "generation_file_id": item["id"],
                        "ref_id": item["ref_id"],
                        "gdrive_file_id": item["gdrive_file_id"],
                        "category": item["category"],
                        "board_id": PINTEREST_BOARD_ID,
                        "scheduled_at": dt.isoformat(),
                    })
                    idx += 1

            day_offset += 1

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
                await db.execute(
                    """INSERT INTO pins_schedule
                       (generation_file_id, ref_id, gdrive_file_id, category, board_id, scheduled_at, status)
                       VALUES (?, ?, ?, ?, ?, ?, 'pending')""",
                    (item["id"], item["ref_id"], item["gdrive_file_id"],
                     item["category"], PINTEREST_BOARD_ID, dt.isoformat()),
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


async def _ensure_today_quota():
    """Accelerate enough future pins to fill today's quota (IMAGES_PER_DAY_MIN)."""
    now_dt = datetime.now(tz)
    if not _in_pin_window(now_dt):
        return

    today_end_dt = _pin_window_end(now_dt)
    today_end = today_end_dt.isoformat()
    today_start = now_dt.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        published_today = await _count_published_today(db, now_dt)
        async with db.execute(
            "SELECT COUNT(*) FROM pins_schedule WHERE status = 'pending' AND scheduled_at >= ? AND scheduled_at <= ?",
            (today_start, today_end),
        ) as cur:
            pending_today = (await cur.fetchone())[0]

        needed = max(0, IMAGES_PER_DAY_MIN - published_today - pending_today)
        if needed > 0:
            async with db.execute(
                "SELECT id FROM pins_schedule WHERE status = 'pending' AND scheduled_at > ? "
                "ORDER BY scheduled_at LIMIT ?",
                (today_end, needed),
            ) as cur:
                future_pins = await cur.fetchall()
            if future_pins:
                # Spread pulled pins evenly from now to end of day
                # Минимум 30 минут между пинами — чтобы не было залпа вечером
                remaining_seconds = max(0, (today_end_dt - now_dt).total_seconds())
                count = len(future_pins)
                interval = max(30 * 60, remaining_seconds / max(count, 1))
                for i, p in enumerate(future_pins):
                    slot_dt = now_dt + timedelta(seconds=i * interval)
                    await db.execute(
                        "UPDATE pins_schedule SET scheduled_at = ? WHERE id = ?",
                        (slot_dt.isoformat(), p[0]),
                    )
            await db.commit()


async def publish_due_pins(bot, admin_chat_id: int):
    """Called by APScheduler every minute. Publishes pins whose scheduled_at has passed."""
    now_dt = datetime.now(tz)
    now = now_dt.isoformat()

    if not _in_pin_window(now_dt):
        moved = await _defer_due_pins_to_next_window(now_dt)
        if moved:
            logger.info(f"Moved {moved} due pins to next allowed window.")
        await _check_posting_completion(bot, admin_chat_id)
        return

    # Жёсткий лимит: не более DAILY_PIN_HARD_LIMIT пинов в сутки
    async with aiosqlite.connect(DB_PATH) as db:
        published_today = await _count_published_today(db, now_dt)
    # Soft daily cap by schedule policy (e.g., 10/day). Hard limit remains a safety fuse.
    if published_today >= IMAGES_PER_DAY_MAX:
        await _check_posting_completion(bot, admin_chat_id)
        return
    if published_today >= DAILY_PIN_HARD_LIMIT:
        logger.warning(f"Daily hard limit {DAILY_PIN_HARD_LIMIT} reached ({published_today} published). Skipping tick.")
        await _check_posting_completion(bot, admin_chat_id)
        return

    # Проверяем квоту в начале каждого тика — если день пустой, заполним
    await _ensure_today_quota()

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM pins_schedule WHERE status = 'pending' AND scheduled_at <= ? "
            "ORDER BY scheduled_at ASC LIMIT 1",
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
        ref_id = row["ref_id"]
        if ok:
            pass
        else:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE pins_schedule SET status = 'failed' WHERE id = ?", (row["id"],)
                )
                await db.commit()
        # Check TG trigger after both success and failure
        if ref_id:
            await _check_ref_tg_trigger(ref_id)
        await asyncio.sleep(DELAY_MAKE_WEBHOOK)

    await _check_posting_completion(bot, admin_chat_id)


async def _check_ref_tg_trigger(ref_id: int):
    """If at least one pin for ref_id is published, schedule a TG post."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Check if at least one pin is published
        async with db.execute(
            "SELECT COUNT(*) as done FROM pins_schedule WHERE ref_id = ? AND status = 'published'",
            (ref_id,),
        ) as cur:
            done_row = await cur.fetchone()

        done = done_row["done"] or 0 if done_row else 0

        if done == 0:
            return

        # Check if TG post already exists for this ref (pending or already posted)
        async with db.execute(
            "SELECT id FROM tg_posts WHERE ref_id = ? AND status IN ('pending', 'pending_approval', 'posted')", (ref_id,)
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
            p = prompts[0] if prompts else {}
            cat = row["category"].lower()
            if "нейрофото" in cat:
                # Show version without glasses for public TG post
                prompt_text = p.get("full_no_glasses") or p.get("full", "")
            else:
                prompt_text = p.get("full", "")
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

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM pins_schedule WHERE status = 'pending'"
        ) as cur:
            row = await cur.fetchone()
    pending_pins = row[0] if row else 0

    if pending_pins == 0:
        await set_state(posting_status="done")
        await bot.send_message(
            chat_id,
            "Постинг завершён. Все пины опубликованы.\n\nЗапустить новый цикл? → /pinterest_analyze"
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
