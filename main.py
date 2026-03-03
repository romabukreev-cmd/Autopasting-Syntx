import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

from config import BOT_TOKEN, DB_PATH, TIMEZONE
from database import init_db
from modules.bot import router

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

    jobstores = {"default": SQLAlchemyJobStore(url=f"sqlite:///{DB_PATH}")}
    scheduler = AsyncIOScheduler(jobstores=jobstores, timezone=TIMEZONE)

    from config import ADMIN_USER_ID
    from modules.scheduler import publish_due_pins, cleanup_old_pinterest_files

    # Publish due pins every minute
    scheduler.add_job(
        publish_due_pins,
        trigger="interval",
        minutes=1,
        args=[bot, ADMIN_USER_ID],
        id="publish_due_pins",
        replace_existing=True,
    )

    # Daily cleanup of old pinterest files (03:00 МСК)
    scheduler.add_job(
        cleanup_old_pinterest_files,
        trigger="cron",
        hour=3,
        minute=0,
        id="cleanup_pinterest",
        replace_existing=True,
    )

    scheduler.start()

    logger.info("Bot started")
    try:
        await dp.start_polling(bot, scheduler=scheduler)
    finally:
        scheduler.shutdown()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
