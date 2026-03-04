import asyncio
import base64
import json
import logging
from datetime import date

import aiosqlite
from openai import AsyncOpenAI

from config import (
    DB_PATH,
    DELAY_GDRIVE_DOWNLOAD,
    DRIVE_BASE_PATH,
    DRIVE_FOLDER_REFS,
    IMAGES_PER_WEEK,
    OPENROUTER_API_KEY,
    OPENROUTER_BASE_URL,
    MODEL_ANALYZER,
)
from database import set_state
from modules import drive

logger = logging.getLogger(__name__)

client = AsyncOpenAI(api_key=OPENROUTER_API_KEY, base_url=OPENROUTER_BASE_URL)

ANALYZE_PROMPT = """You are analyzing a reference image to create image generation prompts.

STEP 1 — Extract the base prompt:
Look for any generation prompt text written on the image (usually at the bottom, in a text block).
- If you can read it clearly → use it EXACTLY as the base_prompt, word for word.
- If the text is too small, blurry, or unreadable → describe the image visually and write a detailed generation prompt yourself that would recreate the same result.

STEP 2 — Create 5 variants:
Using the base_prompt, create 5 variations where ONLY the pose or camera angle changes.
Everything else stays IDENTICAL: subject, appearance, hair, clothes, colors, lighting, style, mood, background, quality settings.

Variant rules:
- Variant 1: exact original pose (unchanged)
- Variant 2: different body pose (e.g., sitting, walking, different hand position)
- Variant 3: different camera angle (e.g., side profile, three-quarter view, from behind)
- Variant 4: different framing/distance (e.g., close-up portrait vs full body)
- Variant 5: different pose detail (e.g., looking away, turned head, different arm position)

For each variant:
- "full": complete generation prompt up to 1000 characters
- "short": 80-120 character summary for text overlay on the image

Return JSON only:
{
  "base_prompt": "...",
  "variants": [
    {"full": "...", "short": "..."},
    {"full": "...", "short": "..."},
    {"full": "...", "short": "..."},
    {"full": "...", "short": "..."},
    {"full": "...", "short": "..."}
  ]
}"""


async def _analyze_image(image_data: bytes) -> dict:
    b64 = base64.b64encode(image_data).decode()
    resp = await client.chat.completions.create(
        model=MODEL_ANALYZER,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": ANALYZE_PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ],
        }],
        response_format={"type": "json_object"},
        max_tokens=4000,
    )
    return json.loads(resp.choices[0].message.content)


async def run_analysis(bot, chat_id: int):
    try:
        refs_base = f"{DRIVE_BASE_PATH}/{DRIVE_FOLDER_REFS}"

        # List category subfolders
        categories = await drive.list_dirs(refs_base)
        if not categories:
            await bot.send_message(chat_id, f"Папка '{refs_base}' пуста или не найдена на Google Drive.")
            await set_state(analysis_status="idle")
            return

        # Load existing refs from DB
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT gdrive_file_id, md5 FROM refs") as cur:
                existing = {row["gdrive_file_id"]: row["md5"] async for row in cur}

        # Collect all image files
        all_refs = []
        for cat in categories:
            cat_path = f"{refs_base}/{cat['name']}"
            files = await drive.list_files(cat_path)
            images = [f for f in files if "image" in f.get("mime_type", "") or
                      f["name"].lower().endswith((".jpg", ".jpeg", ".png", ".webp"))]
            for img in images:
                img["category"] = cat["name"]
                img["path"] = f"{cat_path}/{img['name']}"
            all_refs.extend(images)

        total = len(all_refs)
        if total == 0:
            await bot.send_message(chat_id, "Референсы не найдены. Загрузи изображения в папки Референсов на Drive.")
            await set_state(analysis_status="idle")
            return

        # Determine what's new or changed
        to_process = []
        skipped = 0
        for ref in all_refs:
            file_id = ref["id"]
            drive_md5 = ref.get("md5", "")
            if file_id not in existing:
                to_process.append(ref)
            elif drive_md5 and drive_md5 != existing[file_id]:
                to_process.append(ref)
            else:
                skipped += 1

        new_count = len(to_process)
        await bot.send_message(
            chat_id,
            f"Найдено {total} референсов. Новых: {new_count}. Уже обработанных: {skipped}.\n"
            f"Начинаю анализ {new_count} новых..."
        )

        if not to_process:
            await set_state(analysis_status="done")
            return

        processed = 0
        for ref in to_process:
            try:
                data = await drive.download_file(ref["path"])
                md5 = await drive.compute_md5(data)

                result = await _analyze_image(data)
                variants = result.get("variants", [])
                base_prompt = result.get("base_prompt", "")

                # Log for debugging
                logger.info(f"Analyzed '{ref['name']}': base_prompt={base_prompt[:150]}")
                for i, v in enumerate(variants):
                    logger.info(f"  Variant {i}: {v.get('full', '')[:120]}")

                prompts_json = json.dumps(variants, ensure_ascii=False)
                today = date.today().isoformat()

                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("DELETE FROM refs WHERE gdrive_file_id = ?", (ref["id"],))
                    await db.execute(
                        """INSERT INTO refs (filename, category, gdrive_file_id, md5, processed_at, prompts)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (ref["name"], ref["category"], ref["id"], md5, today, prompts_json),
                    )
                    await db.commit()

                processed += 1
                await asyncio.sleep(DELAY_GDRIVE_DOWNLOAD)

            except Exception as e:
                logger.error(f"Error processing {ref['name']}: {e}")

        total_prompts = processed * 5
        weeks = round(total_prompts / IMAGES_PER_WEEK, 1)

        await bot.send_message(
            chat_id,
            f"Готово. Создано {total_prompts} промптов ({processed} × 5 вариантов позы/ракурса).\n"
            f"Распределено по {weeks} неделям.\n\n"
            f"Запустить генерацию → Pinterest → Генерация неделя 1"
        )
        await set_state(analysis_status="done")

    except Exception as e:
        logger.error(f"Analysis failed: {e}")
        await bot.send_message(chat_id, f"Ошибка анализа: {e}")
        await set_state(analysis_status="idle")
