import asyncio
import logging

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
)

from config import ADMIN_USER_ID
from database import get_state, set_state

router = Router()
logger = logging.getLogger(__name__)

async def _show_menu(bot, chat_id: int, text: str, markup) -> None:
    """Edit the persistent menu message in place, or send a new one."""
    state = await get_state()
    menu_msg_id = state.get("menu_msg_id")
    if menu_msg_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=menu_msg_id,
                text=text, reply_markup=markup,
            )
            return
        except Exception:
            pass
    sent = await bot.send_message(chat_id, text, reply_markup=markup)
    await set_state(menu_msg_id=sent.message_id)


# --- Keyboards ---

def kb_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Pinterest", callback_data="menu:pinterest")],
        [InlineKeyboardButton(text="Telegram", callback_data="menu:telegram")],
        [InlineKeyboardButton(text="ВКонтакте", callback_data="menu:vk")],
    ])


def kb_pinterest(week: int = 1):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Анализ референсов", callback_data="pin:analyze")],
        [
            InlineKeyboardButton(text="◀", callback_data=f"pin:week:{max(1, week - 1)}"),
            InlineKeyboardButton(text=f"Неделя {week}", callback_data=f"pin:week:{week}"),
            InlineKeyboardButton(text="▶", callback_data=f"pin:week:{week + 1}"),
        ],
        [InlineKeyboardButton(text=f"▶ Генерация недели {week}", callback_data=f"pin:generate:{week}")],
        [InlineKeyboardButton(text="Запустить постинг", callback_data="pin:start")],
        [InlineKeyboardButton(text="Повторить упавшие", callback_data="pin:retry")],
        [
            InlineKeyboardButton(text="Статус", callback_data="pin:status"),
            InlineKeyboardButton(text="Сбросить статусы", callback_data="pin:reset"),
        ],
        [InlineKeyboardButton(text="🗑 Очистить всё", callback_data="pin:clear")],
        [InlineKeyboardButton(text="← Назад", callback_data="menu:main")],
    ])


def kb_soon():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="← Назад", callback_data="menu:main")],
    ])


def kb_reply_main():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Pinterest"), KeyboardButton(text="Telegram"), KeyboardButton(text="ВКонтакте")],
        ],
        resize_keyboard=True,
        persistent=True,
    )




# --- Admin guard ---

def admin_only(func):
    from functools import wraps

    @wraps(func)
    async def wrapper(event, **kwargs):
        user_id = event.from_user.id if hasattr(event, "from_user") else None
        if ADMIN_USER_ID and user_id != ADMIN_USER_ID:
            return
        return await func(event, **kwargs)

    return wrapper


# --- /start ---

@router.message(Command("start"))
@admin_only
async def cmd_start(message: Message):
    await set_state(menu_msg_id=None)
    await message.answer("Контент-завод Syntx", reply_markup=kb_reply_main())
    await _show_menu(message.bot, message.chat.id, "Выбери площадку:", kb_main())


# --- Main menu navigation ---

@router.callback_query(F.data == "menu:main")
@admin_only
async def cb_menu_main(call: CallbackQuery):
    await call.message.edit_text("Контент-завод Syntx\n\nВыбери площадку:", reply_markup=kb_main())


@router.callback_query(F.data == "menu:pinterest")
@admin_only
async def cb_menu_pinterest(call: CallbackQuery):
    state = await get_state()
    week = state.get("active_week") or 1
    await call.message.edit_text("Pinterest", reply_markup=kb_pinterest(week))


def kb_telegram():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Статус ТГ-постинга", callback_data="tg:status")],
        [InlineKeyboardButton(text="← Назад", callback_data="menu:main")],
    ])


@router.callback_query(F.data == "menu:telegram")
@admin_only
async def cb_menu_telegram(call: CallbackQuery):
    await call.message.edit_text("Telegram", reply_markup=kb_telegram())


@router.callback_query(F.data == "tg:status")
@admin_only
async def cb_tg_status(call: CallbackQuery):
    import aiosqlite
    from config import DB_PATH
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT COUNT(*) as total, "
            "SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) as pending, "
            "SUM(CASE WHEN status='posted' THEN 1 ELSE 0 END) as posted "
            "FROM tg_posts"
        ) as cur:
            row = await cur.fetchone()
    total = row["total"] or 0
    pending = row["pending"] or 0
    posted = row["posted"] or 0
    await call.answer()
    await call.message.answer(
        f"Telegram постинг\n\n"
        f"Всего постов: {total}\n"
        f"Ожидают публикации: {pending}\n"
        f"Опубликовано: {posted}"
    )


@router.callback_query(F.data == "menu:vk")
@admin_only
async def cb_menu_vk(call: CallbackQuery):
    await call.message.edit_text("ВКонтакте — скоро", reply_markup=kb_soon())


# --- Week switcher ---

@router.callback_query(F.data.startswith("pin:week:"))
@admin_only
async def cb_week(call: CallbackQuery):
    week = int(call.data.split(":")[2])
    await set_state(active_week=week)
    await call.message.edit_reply_markup(reply_markup=kb_pinterest(week))


# --- Pinterest actions ---

@router.callback_query(F.data == "pin:analyze")
@admin_only
async def cb_analyze(call: CallbackQuery):
    state = await get_state()
    if state.get("analysis_status") == "running":
        await call.answer("Анализ уже выполняется.", show_alert=True)
        return
    await set_state(analysis_status="running")
    await call.answer("Запускаю анализ...")
    await call.message.answer("Запускаю анализ референсов...")
    from modules.analyzer import run_analysis
    asyncio.create_task(run_analysis(call.bot, call.message.chat.id))


@router.callback_query(F.data.startswith("pin:generate:"))
@admin_only
async def cb_generate(call: CallbackQuery):
    state = await get_state()
    if state.get("generation_status") == "running":
        await call.answer("Генерация уже выполняется.", show_alert=True)
        return
    week = int(call.data.split(":")[2])
    await set_state(generation_status="running", active_week=week)
    await call.answer(f"Запускаю неделю {week}...")
    await call.message.answer(f"Запускаю генерацию для недели {week}...")
    from modules.generator import run_generation
    asyncio.create_task(run_generation(call.bot, call.message.chat.id, week))


@router.callback_query(F.data == "pin:start")
@admin_only
async def cb_start_posting(call: CallbackQuery):
    state = await get_state()
    if state.get("generation_status") not in ("done", "partial"):
        await call.answer("Сначала завершите генерацию.", show_alert=True)
        return
    await call.answer("Добавляю пины в расписание...")
    await call.message.answer("Добавляю новые пины в расписание постинга...")
    from modules.scheduler import setup_posting_schedule
    asyncio.create_task(setup_posting_schedule(call.bot, call.message.chat.id))


@router.callback_query(F.data == "pin:retry")
@admin_only
async def cb_retry(call: CallbackQuery):
    state = await get_state()
    if state.get("generation_status") == "running":
        await call.answer("Генерация уже выполняется.", show_alert=True)
        return
    await set_state(generation_status="running")
    await call.answer("Запускаю повтор...")
    await call.message.answer("Запускаю повторную генерацию для упавших...")
    from modules.generator import run_retry
    asyncio.create_task(run_retry(call.bot, call.message.chat.id))


@router.callback_query(F.data == "pin:status")
@admin_only
async def cb_status(call: CallbackQuery):
    state = await get_state()
    lines = [
        "Статус Pinterest\n",
        f"Активная неделя: {state.get('active_week', 0)}",
        f"Анализ: {state.get('analysis_status', 'idle')}",
        f"Генерация: {state.get('generation_status', 'idle')}",
        f"Постинг: {state.get('posting_status', 'idle')}",
    ]
    if state.get("posting_start_date"):
        lines.append(f"Начат: {state['posting_start_date']}")
    if state.get("posting_end_date"):
        lines.append(f"Окончание: {state['posting_end_date']}")
    import aiosqlite
    from config import DB_PATH
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT "
            "SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) as pending, "
            "SUM(CASE WHEN status='published' THEN 1 ELSE 0 END) as published, "
            "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed "
            "FROM pins_schedule"
        ) as cur:
            row = await cur.fetchone()
    if row:
        lines.append(
            f"\nПинов: ожидают {row['pending'] or 0} · опубликовано {row['published'] or 0} · пропущено {row['failed'] or 0}"
        )
    await call.answer()
    await call.message.answer("\n".join(lines))


@router.callback_query(F.data == "pin:reset")
@admin_only
async def cb_reset(call: CallbackQuery):
    await set_state(analysis_status="idle", generation_status="idle", posting_status="idle")
    await call.answer("Статусы сброшены", show_alert=True)
    state = await get_state()
    week = state.get("active_week") or 1
    try:
        await call.message.edit_text("Pinterest", reply_markup=kb_pinterest(week))
    except Exception:
        pass


# --- Legacy text commands (still work) ---

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
    if state.get("generation_status") not in ("done", "partial"):
        await message.answer("Сначала завершите генерацию изображений.")
        return
    await message.answer("Добавляю новые пины в расписание постинга...")
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


# --- pin:clear — wipe everything (Drive + all DB tables + statuses) ---

@router.callback_query(F.data == "pin:clear")
@admin_only
async def cb_clear(call: CallbackQuery):
    await call.answer("Очищаю...", show_alert=False)
    await call.message.answer("Очищаю всё: Drive, базу, рефы, промпты, расписание...")
    import aiosqlite
    from config import DB_PATH, DRIVE_BASE_PATH, DRIVE_FOLDER_GENS
    from modules import drive

    try:
        gens_path = f"{DRIVE_BASE_PATH}/{DRIVE_FOLDER_GENS}"
        await drive.purge_folder(gens_path)

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM generation_files")
            await db.execute("DELETE FROM generations")
            await db.execute("DELETE FROM pins_schedule")
            await db.execute("DELETE FROM tg_posts")
            await db.execute("DELETE FROM refs")
            await db.commit()

        await set_state(
            analysis_status="idle",
            generation_status="idle",
            posting_status="idle",
            posting_start_date=None,
            posting_end_date=None,
        )

        state = await get_state()
        week = state.get("active_week") or 1
        await call.message.answer("Готово. Всё очищено. Можно загружать новые референсы.")
        try:
            await call.message.edit_text("Pinterest", reply_markup=kb_pinterest(week))
        except Exception:
            pass

    except Exception as e:
        logger.error(f"Clear failed: {e}")
        await call.message.answer(f"Ошибка при очистке: {e}")


# --- Reply keyboard text handlers ---

@router.message(F.text == "Pinterest")
@admin_only
async def reply_pinterest(message: Message):
    state = await get_state()
    week = state.get("active_week") or 1
    await _show_menu(message.bot, message.chat.id, "Pinterest", kb_pinterest(week))


@router.message(F.text == "Telegram")
@admin_only
async def reply_telegram(message: Message):
    await _show_menu(message.bot, message.chat.id, "Telegram", kb_telegram())


@router.message(F.text == "ВКонтакте")
@admin_only
async def reply_vk(message: Message):
    await _show_menu(message.bot, message.chat.id, "ВКонтакте — скоро", kb_soon())
