import asyncio
import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from config import ADMIN_USER_ID
from database import get_state, set_state

router = Router()
logger = logging.getLogger(__name__)


def admin_only(func):
    from functools import wraps

    @wraps(func)
    async def wrapper(message: Message, **kwargs):
        if ADMIN_USER_ID and message.from_user.id != ADMIN_USER_ID:
            return
        return await func(message, **kwargs)

    return wrapper


@router.message(Command("start"))
@admin_only
async def cmd_start(message: Message):
    await message.answer(
        "Контент-завод Syntx\n\n"
        "Команды:\n"
        "/pinterest_analyze — анализ референсов\n"
        "/pinterest_generate N — генерация недели N\n"
        "/pinterest_start — запустить постинг\n"
        "/pinterest_retry — повторить упавшие\n"
        "/status — текущий статус"
    )


@router.message(Command("status"))
@admin_only
async def cmd_status(message: Message):
    state = await get_state()
    lines = [
        "Статус системы\n",
        f"Активная неделя: {state.get('active_week', 0)}",
        f"Анализ: {state.get('analysis_status', 'idle')}",
        f"Генерация: {state.get('generation_status', 'idle')}",
        f"Постинг: {state.get('posting_status', 'idle')}",
    ]
    if state.get("posting_start_date"):
        lines.append(f"Начат: {state['posting_start_date']}")
    if state.get("posting_end_date"):
        lines.append(f"Окончание: {state['posting_end_date']}")
    await message.answer("\n".join(lines))


@router.message(Command("pinterest_analyze"))
@admin_only
async def cmd_analyze(message: Message):
    state = await get_state()
    if state.get("analysis_status") == "running":
        await message.answer("Анализ уже выполняется.")
        return

    await set_state(analysis_status="running")
    await message.answer("Запускаю анализ референсов...")

    from modules.analyzer import run_analysis
    asyncio.create_task(run_analysis(message.bot, message.chat.id))


@router.message(Command("pinterest_generate"))
@admin_only
async def cmd_generate(message: Message):
    state = await get_state()
    if state.get("generation_status") == "running":
        await message.answer("Генерация уже выполняется.")
        return

    parts = message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Укажи номер недели: /pinterest_generate 1")
        return

    week = int(parts[1])
    await set_state(generation_status="running", active_week=week)
    await message.answer(f"Запускаю генерацию для недели {week}...")

    from modules.generator import run_generation
    asyncio.create_task(run_generation(message.bot, message.chat.id, week))


@router.message(Command("pinterest_start"))
@admin_only
async def cmd_start_posting(message: Message):
    state = await get_state()
    if state.get("posting_status") == "running":
        await message.answer("Постинг уже активен. Дождись окончания.")
        return
    if state.get("generation_status") not in ("done", "partial"):
        await message.answer("Сначала завершите генерацию изображений.")
        return

    await message.answer("Составляю расписание постинга...")

    from modules.scheduler import setup_posting_schedule
    asyncio.create_task(setup_posting_schedule(message.bot, message.chat.id))


@router.message(Command("pinterest_retry"))
@admin_only
async def cmd_retry(message: Message):
    state = await get_state()
    if state.get("generation_status") == "running":
        await message.answer("Генерация уже выполняется.")
        return

    await set_state(generation_status="running")
    await message.answer("Запускаю повторную генерацию для упавших изображений...")

    from modules.generator import run_retry
    asyncio.create_task(run_retry(message.bot, message.chat.id))
