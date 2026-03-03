import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import BOT_TOKEN, ADMIN_USER_ID, TIMEZONE
from database import init_db
from modules.bot import router
import modules.scheduler as sched_module

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def main():
    await init_db()

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    # Store bot reference for scheduler jobs to use
    sched_module._bot = bot
    sched_module._admin_chat_id = ADMIN_USER_ID

    # Use memory jobstore — recurring jobs are re-registered on each start.
    # Actual pin schedule is persisted in SQLite pins_schedule table.
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)

    scheduler.add_job(
        sched_module.publish_due_pins_job,
        trigger="interval",
        minutes=1,
        id="publish_due_pins",
        replace_existing=True,
    )

    scheduler.add_job(
        sched_module.cleanup_old_pinterest_files,
        trigger="cron",
        hour=3,
        minute=0,
        id="cleanup_pinterest",
        replace_existing=True,
    )

    scheduler.start()

    logger.info("Bot started")
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
