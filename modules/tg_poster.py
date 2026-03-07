import html
import logging
import random
from datetime import datetime, timezone

import aiosqlite
from aiogram.types import BufferedInputFile, InputMediaPhoto
from openai import AsyncOpenAI

from config import (
    DB_PATH,
    MODEL_TG_POST,
    OPENROUTER_API_KEY,
    OPENROUTER_BASE_URL,
    TG_CHANNEL_ID,
)
from modules import drive

logger = logging.getLogger(__name__)

client = AsyncOpenAI(api_key=OPENROUTER_API_KEY, base_url=OPENROUTER_BASE_URL)

TG_POST_PROMPT = """Ты — копирайтер Telegram-канала про нейросети и промпты. Твоя задача — написать короткую подводку к посту с промптом для генерации изображения.

Ты получаешь промпт генерации изображения. Проанализируй его, пойми что за визуал он создаёт, и напиши:

1. ЗАГОЛОВОК — формат: "ПРОМПТ · [НАЗВАНИЕ СТИЛЯ/ЭФФЕКТА В ВЕРХНЕМ РЕГИСТРЕ]"
   Название должно быть коротким (2-4 слова), цепляющим, отражать суть визуала. Не повторяй слова из промпта дословно — переосмысли.

2. ВСТУПЛЕНИЕ — 1-3 предложения. Это НЕ описание картинки. Это:
   - или формат/приём и для кого он ("Формат для тех, кто хочет...")
   - или короткий комментарий почему это работает
   - или провокация/интрига ("Один промпт — и обычное фото превращается в...")
   - или практическая подача ("Готовый промпт для...")

Меняй стиль вступления от поста к посту. Не начинай каждый раз одинаково. Варьируй: где-то одно предложение, где-то два-три. Где-то сухо и по делу, где-то с лёгкой эмоцией.

ПРАВИЛА:
- Пиши на русском
- Без эмодзи (кроме случаев где 1 эмодзи действительно уместен)
- Без восклицательных знаков
- Без слов: "уникальный", "потрясающий", "невероятный", "магия", "волшебство"
- Без прямых продаж и CTA (инструкция добавляется отдельно)
- Тон: простой, живой, как в личном блоге. Не экспертный, не продающий — просто человек делится находкой. Можно начать с "Тот случай, когда...", "Короче,", "Вот это я понимаю —", "Простой промпт, а результат...". Без пафоса, без умных слов, без editorial-жаргона.
- НЕ описывай изображение буквально — дай контекст, приём, идею

ФОРМАТ ОТВЕТА (строго):

ПРОМПТ · [НАЗВАНИЕ]

[Вступление]

Больше ничего не пиши. Никаких пояснений, никакого промпта, никакой инструкции — только заголовок и вступление."""

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


def _build_post(header_intro: str, prompt_text: str, category: str) -> str:
    # Parse Claude response: first line = header, rest = intro
    parts_raw = header_intro.split("\n\n", 1)
    header = parts_raw[0].strip()
    intro = parts_raw[1].strip() if len(parts_raw) > 1 else ""

    # Normalize category for dict lookups (refs table stores short names)
    full_cat = category if category.startswith("ПРОМПТЫ") else f"ПРОМПТЫ / {category}"

    parts = [
        f"<b>⬆️ {html.escape(header)}</b>",
        html.escape(intro) if intro else None,
        "<b>Копируй промпт \U0001f447</b>",
        f"<pre>{html.escape(prompt_text)}</pre>",
    ]
    parts = [p for p in parts if p]

    instruction_key = category if category in INSTRUCTION_CATEGORIES else full_cat
    if instruction_key in INSTRUCTION_CATEGORIES:
        parts.append(_build_instruction(INSTRUCTION_CATEGORIES[instruction_key]))

    hashtag = CATEGORY_HASHTAGS.get(category) or CATEGORY_HASHTAGS.get(full_cat, "")
    if hashtag:
        parts.append(hashtag)

    return "\n\n".join(parts)


async def _pick_images(ref_id: int) -> tuple[int, list[bytes]]:
    """Pick images by random scenario. Returns (scenario, list_of_image_bytes)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT filename, model, type FROM generation_files "
            "WHERE ref_id = ? ORDER BY id",
            (ref_id,),
        ) as cur:
            files = [dict(r) for r in await cur.fetchall()]

    clean_files = [f for f in files if f["type"] == "clean"]
    pin_files = [f for f in files if f["type"] == "pin"]

    # Scenario weights: 1 clean (40%), 3 clean (40%), 1 pin (20%)
    scenario = random.choices([1, 2, 3], weights=[40, 40, 20])[0]

    if scenario == 1:
        chosen = random.sample(clean_files, min(1, len(clean_files)))
    elif scenario == 2:
        chosen = random.sample(clean_files, min(3, len(clean_files)))
    else:
        chosen = random.sample(pin_files, min(1, len(pin_files)))

    images = []
    for f in chosen:
        try:
            data = await drive.download_file(f["filename"])
            images.append(data)
        except Exception as e:
            logger.warning(f"Failed to download {f['filename']}: {e}")

    return scenario, images


async def post_tg(bot, tg_post_id: int, ref_id: int, prompt: str, category: str):
    """Generate post text, pick images, post to TG channel, update DB."""
    logger.info(f"Posting TG post tg_post_id={tg_post_id} ref_id={ref_id}")

    header_intro = await _generate_header_intro(prompt, category)
    text = _build_post(header_intro, prompt, category)
    scenario, images = await _pick_images(ref_id)

    try:
        if not images:
            await bot.send_message(TG_CHANNEL_ID, text, parse_mode="HTML")
        elif len(images) == 1:
            photo = BufferedInputFile(images[0], filename="image.jpg")
            await bot.send_photo(TG_CHANNEL_ID, photo=photo)
            await bot.send_message(TG_CHANNEL_ID, text, parse_mode="HTML")
        else:
            media = []
            for i, img_bytes in enumerate(images):
                photo = BufferedInputFile(img_bytes, filename=f"image_{i}.jpg")
                media.append(InputMediaPhoto(media=photo))
            await bot.send_media_group(TG_CHANNEL_ID, media=media)
            await bot.send_message(TG_CHANNEL_ID, text, parse_mode="HTML")

        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE tg_posts SET status = 'posted', scenario = ?, posted_at = ? WHERE id = ?",
                (scenario, now, tg_post_id),
            )
            await db.commit()
        logger.info(f"TG post {tg_post_id} published (scenario={scenario})")

    except Exception as e:
        logger.error(f"Failed to send TG post {tg_post_id}: {e}")
        raise
