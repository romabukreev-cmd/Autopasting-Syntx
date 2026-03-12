import html
import logging
import random
from datetime import datetime, timezone

import aiosqlite
from aiogram.types import (
    BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
    InputMediaPhoto, LinkPreviewOptions,
)
from openai import AsyncOpenAI

from config import (
    ADMIN_USER_ID,
    DB_PATH,
    MODEL_TG_POST,
    OPENROUTER_API_KEY,
    OPENROUTER_BASE_URL,
    TG_CHANNEL_ID,
)
from modules import drive

logger = logging.getLogger(__name__)

client = AsyncOpenAI(api_key=OPENROUTER_API_KEY, base_url=OPENROUTER_BASE_URL)

# Pending posts awaiting admin approval: tg_post_id → {images, main_text, prompt_block, scenario}
_PENDING: dict[int, dict] = {}

TG_CAPTION_LIMIT = 1024  # Telegram photo caption limit

TG_POST_PROMPT = """Ты — копирайтер Telegram-канала про нейросети и промпты.
Твоя задача — написать короткую подводку к посту с промптом для генерации изображения.

Ты получаешь промпт генерации. Проанализируй его и напиши:

1. ЗАГОЛОВОК — формат: "ПРОМПТ · [НАЗВАНИЕ В ВЕРХНЕМ РЕГИСТРЕ]"
   2-4 слова, отражает суть визуала. Не копируй слова из промпта — переосмысли.

2. ВСТУПЛЕНИЕ — 2-3 коротких предложения. Объясни:
   - какой визуальный эффект или приём создаёт этот промпт
   - в каких ситуациях или для каких задач он подходит
   - что в нём неочевидного или почему это работает

   Каждый пост — другая точка входа. Используй один из режимов:
   — ПРИЁМ: объясни техническую идею простыми словами
   — СИТУАЦИЯ: скажи когда и для чего это использовать
   — НАБЛЮДЕНИЕ: короткий вывод про то, почему результат выглядит так, а не иначе
   — СРАВНЕНИЕ: с чем это ассоциируется из реального мира (плёнка, живопись, кино)

ПРАВИЛА:
- Русский язык, без эмодзи
- Без восклицательных знаков
- Без слов: "уникальный", "потрясающий", "невероятный", "магия", "атмосфера", "буквально"
- Без CTA и прямых продаж
- Тон: просто, как человек объясняет другу
- Короткие предложения. Лучше недосказать, чем перегрузить

ФОРМАТ ОТВЕТА (строго):

ПРОМПТ · [НАЗВАНИЕ]

[Вступление]

Больше ничего не пиши — только заголовок и вступление."""

CATEGORY_HASHTAGS = {
    "ПРОМПТЫ / Мужские нейрофото": "#мужскоенейрофото",
    "ПРОМПТЫ / Женские нейрофото": "#женскоенейрофото",
    "ПРОМПТЫ / 3D буквы": "#3Dбуквы",
    "ПРОМПТЫ / 3D логотипы": "#3Dлоготипы",
    "ПРОМПТЫ / 3D Текст": "#3Dтекст",
    "ПРОМПТЫ / Персонажи": "#персонажи",
    "ПРОМПТЫ / Фото товаров": "#нейрофототовара",
    "ПРОМПТЫ / Эстетика": "#нейроэстетика",
}

INSTRUCTION_CATEGORIES = {
    "ПРОМПТЫ / Мужские нейрофото": "своё фото",
    "ПРОМПТЫ / Женские нейрофото": "своё фото",
    "ПРОМПТЫ / Фото товаров": "фото товара",
}


def _build_instruction(photo_text: str) -> str:
    return (
        "<b>Как сделать:</b>\n\n"
        '1\u20e3 Открой <a href="https://t.me/syntxaibot?start=aff_359133225"><b>бот Syntx</b></a>\n\n'
        "2\u20e3 Жми в «Дизайн с ИИ» → Nano Banana 2 / Seedream 4.5\n\n"
        f"3\u20e3 Прикрепи {photo_text}\n\n"
        "4\u20e3 Выбери формат и качество изображения\n\n"
        "5\u20e3 Вставь готовый промпт и отправь"
    )


async def _generate_header_intro(prompt: str, category: str) -> str:
    resp = await client.chat.completions.create(
        model=MODEL_TG_POST,
        messages=[{
            "role": "user",
            "content": f"{TG_POST_PROMPT}\n\nПромпт:\n{prompt}\n\nКатегория: {category}",
        }],
        max_tokens=400,
    )
    return resp.choices[0].message.content.strip()


def _build_post(header_intro: str, prompt_text: str, category: str) -> tuple[str, str]:
    """Returns (main_text, prompt_block). main_text goes as photo caption, prompt_block as separate message."""
    # Parse Claude response: first line = header, rest = intro
    parts_raw = header_intro.split("\n\n", 1)
    header = parts_raw[0].strip()
    intro = parts_raw[1].strip() if len(parts_raw) > 1 else ""

    # Normalize category for dict lookups (refs table stores fullwidth slash ／ from Drive)
    category = category.replace("／", "/")
    full_cat = category if category.startswith("ПРОМПТЫ") else f"ПРОМПТЫ / {category}"

    parts = [
        f"<b>{html.escape(header)}</b>",
        html.escape(intro) if intro else None,
    ]
    parts = [p for p in parts if p]

    instruction_key = category if category in INSTRUCTION_CATEGORIES else full_cat
    if instruction_key in INSTRUCTION_CATEGORIES:
        parts.append(_build_instruction(INSTRUCTION_CATEGORIES[instruction_key]))

    hashtag = CATEGORY_HASHTAGS.get(category) or CATEGORY_HASHTAGS.get(full_cat, "")
    if hashtag:
        parts.append(f"Категория: {hashtag}")

    main_text = "\n\n".join(parts)
    prompt_block = f"<b>Копируй промпт \U0001f447</b>\n\n<pre>{html.escape(prompt_text)}</pre>"

    return main_text, prompt_block


def _combined_caption(main_text: str, prompt_block: str) -> str | None:
    """If main_text + prompt_block fit in TG caption limit, return combined string. Else None."""
    combined = f"{main_text}\n\n{prompt_block}"
    return combined if len(combined) <= TG_CAPTION_LIMIT else None


async def _pick_images(ref_id: int) -> tuple[int, list[bytes]]:
    """Pick images by random scenario. Returns (scenario, list_of_image_bytes).
    Scenario 1: 2 pins (1 NanaBana + 1 SeeDream)
    Scenario 2: 2 clean images
    Scenario 3: 4 clean images
    Scenario 4: 1 clean image (без текста)
    Scenario 5: 1 pin (с текстом)
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT filename, model, type FROM generation_files "
            "WHERE ref_id = ? ORDER BY id",
            (ref_id,),
        ) as cur:
            files = [dict(r) for r in await cur.fetchall()]

    clean_files = [f for f in files if f["type"] == "clean"]
    sd_pins = [f for f in files if f["type"] == "pin" and f["model"] == "seedream"]
    nb_pins = [f for f in files if f["type"] == "pin" and f["model"] == "nanobana"]
    all_pins = sd_pins + nb_pins

    scenario = random.choices([1, 2, 3, 4, 5], weights=[20, 20, 20, 20, 20])[0]

    if scenario == 1:
        # 1 NanaBana pin + 1 SeeDream pin
        chosen = []
        if nb_pins: chosen.append(random.choice(nb_pins))
        if sd_pins: chosen.append(random.choice(sd_pins))
    elif scenario == 2:
        chosen = random.sample(clean_files, min(2, len(clean_files)))
    elif scenario == 3:
        chosen = random.sample(clean_files, min(4, len(clean_files)))
    elif scenario == 4:
        # 1 чистое изображение (без текста)
        chosen = random.sample(clean_files, min(1, len(clean_files)))
    else:
        # 1 пин (с текстом/overlay)
        chosen = [random.choice(all_pins)] if all_pins else []

    images = []
    for f in chosen:
        try:
            data = await drive.download_file(f["filename"])
            images.append(data)
        except Exception as e:
            logger.warning(f"Failed to download {f['filename']}: {e}")

    return scenario, images


async def _send_to_chat(bot, chat_id: int, images: list[bytes], main_text: str,
                        prompt_block: str, extra_markup=None):
    """Send post (images + text) to a given chat. Combines into one message if fits, else two."""
    combined = _combined_caption(main_text, prompt_block)
    caption = combined if combined else main_text

    if len(images) == 1:
        photo = BufferedInputFile(images[0], filename="image.jpg")
        await bot.send_photo(chat_id, photo=photo, caption=caption, parse_mode="HTML",
                             reply_markup=extra_markup if combined else None)
    else:
        media = []
        for i, img_bytes in enumerate(images):
            photo = BufferedInputFile(img_bytes, filename=f"image_{i}.jpg")
            c = caption if i == 0 else None
            media.append(InputMediaPhoto(media=photo, caption=c,
                                         parse_mode="HTML" if c else None))
        await bot.send_media_group(chat_id, media=media)

    if not combined:
        await bot.send_message(chat_id, prompt_block, parse_mode="HTML",
                               link_preview_options=LinkPreviewOptions(is_disabled=True),
                               reply_markup=extra_markup)


async def post_tg(bot, tg_post_id: int, ref_id: int, prompt: str, category: str):
    """Generate post text, pick images, send to admin for approval."""
    logger.info(f"Preparing TG post tg_post_id={tg_post_id} ref_id={ref_id}")

    scenario, images = await _pick_images(ref_id)

    if not images:
        logger.warning(f"No images for tg_post_id={tg_post_id} ref_id={ref_id}, skipping")
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE tg_posts SET status = 'skipped' WHERE id = ?", (tg_post_id,)
            )
            await db.commit()
        return

    header_intro = await _generate_header_intro(prompt, category)
    main_text, prompt_block = _build_post(header_intro, prompt, category)

    # Store for later approval
    _PENDING[tg_post_id] = {
        "images": images,
        "main_text": main_text,
        "prompt_block": prompt_block,
        "scenario": scenario,
    }

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE tg_posts SET status = 'pending_approval', scenario = ? WHERE id = ?",
            (scenario, tg_post_id),
        )
        await db.commit()

    # Send preview to admin
    approve_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Опубликовать", callback_data=f"tg:approve:{tg_post_id}"),
        InlineKeyboardButton(text="❌ Отменить", callback_data=f"tg:cancel:{tg_post_id}"),
    ]])

    await bot.send_message(ADMIN_USER_ID, f"<b>Превью поста #{tg_post_id}</b>", parse_mode="HTML")
    await _send_to_chat(bot, ADMIN_USER_ID, images, main_text, prompt_block,
                        extra_markup=approve_kb)
    logger.info(f"TG post {tg_post_id} sent to admin for approval")


async def publish_approved(bot, tg_post_id: int) -> bool:
    """Called when admin approves. Publishes to TG channel."""
    pending = _PENDING.pop(tg_post_id, None)
    if not pending:
        return False

    images = pending["images"]
    main_text = pending["main_text"]
    prompt_block = pending["prompt_block"]
    scenario = pending["scenario"]

    try:
        await _send_to_chat(bot, TG_CHANNEL_ID, images, main_text, prompt_block)

        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE tg_posts SET status = 'posted', posted_at = ? WHERE id = ?",
                (now, tg_post_id),
            )
            await db.commit()
        logger.info(f"TG post {tg_post_id} published to channel (scenario={scenario})")
        return True

    except Exception as e:
        logger.error(f"Failed to publish approved TG post {tg_post_id}: {e}")
        _PENDING[tg_post_id] = pending  # put back so admin can retry
        raise


async def cancel_post(bot, tg_post_id: int):
    """Called when admin cancels. Marks post as cancelled."""
    _PENDING.pop(tg_post_id, None)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE tg_posts SET status = 'cancelled' WHERE id = ?", (tg_post_id,)
        )
        await db.commit()
    logger.info(f"TG post {tg_post_id} cancelled by admin")
