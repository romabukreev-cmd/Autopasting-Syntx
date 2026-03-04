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

TG_POST_PROMPT = """You are writing a post for a Telegram channel about AI image generation (Syntx AI).

The post is based on this generation prompt:
{prompt}

Category: {category}

Write a Telegram post in Russian with this structure:
1. A catchy headline (bold, use **headline**)
2. The original generation prompt (in a code block using ```)
3. 2-3 short lines: how to use this prompt (where to paste it, what to expect)

Requirements:
- Language: Russian
- Tone: engaging, community-style, no corporate speak
- Total length: 150-300 characters of text (excluding the code block)
- Do NOT add hashtags
- Do NOT add emojis unless they fit naturally

Return only the post text, nothing else."""


async def _generate_post_text(prompt: str, category: str) -> str:
    resp = await client.chat.completions.create(
        model=MODEL_TG_POST,
        messages=[{
            "role": "user",
            "content": TG_POST_PROMPT.format(prompt=prompt, category=category),
        }],
        max_tokens=600,
    )
    return resp.choices[0].message.content.strip()


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
            data = await drive.download_file(f["filename"])  # filename stores full remote path
            images.append(data)
        except Exception as e:
            logger.warning(f"Failed to download {f['filename']}: {e}")

    return scenario, images


async def post_tg(bot, tg_post_id: int, ref_id: int, prompt: str, category: str):
    """Generate post text, pick images, post to TG channel, update DB."""
    logger.info(f"Posting TG post tg_post_id={tg_post_id} ref_id={ref_id}")

    text = await _generate_post_text(prompt, category)
    scenario, images = await _pick_images(ref_id)

    try:
        if not images:
            # Text-only fallback
            await bot.send_message(TG_CHANNEL_ID, text, parse_mode="Markdown")
        elif len(images) == 1:
            photo = BufferedInputFile(images[0], filename="image.jpg")
            await bot.send_photo(TG_CHANNEL_ID, photo=photo, caption=text, parse_mode="Markdown")
        else:
            # Media group — first image gets the caption
            media = []
            for i, img_bytes in enumerate(images):
                photo = BufferedInputFile(img_bytes, filename=f"image_{i}.jpg")
                if i == 0:
                    media.append(InputMediaPhoto(media=photo, caption=text, parse_mode="Markdown"))
                else:
                    media.append(InputMediaPhoto(media=photo))
            await bot.send_media_group(TG_CHANNEL_ID, media=media)

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
