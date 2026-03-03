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

ANALYZE_PROMPT = """You are analyzing a reference image that contains both a visual composition and possibly a visible text prompt (e.g. a Midjourney or Leonardo screenshot).

Your task:
1. OCR: read any text visible on the image (full prompt if present)
2. Visual description: describe the style, composition, subjects, colors, mood
3. Decide on a flag:
   - "match": the OCR text clearly describes what's shown → it IS the generation prompt
   - "partial": OCR text partially relates to the visual, but has errors or is incomplete
   - "no_match": no readable text, or text is unrelated to the visual content

Return JSON only:
{
  "ocr_text": "...",
  "visual_description": "...",
  "flag": "match|partial|no_match",
  "base_prompt": "..."  // the best prompt to use based on flag logic
}"""

VARIANTS_PROMPT = """You are a creative AI art director. Given a base image generation prompt, create 5 variations.

Variation rules:
- #1: original (unchanged)
- #2: change gender/character type if applicable, keep composition
- #3: change color palette significantly
- #4: change pose/angle/perspective
- #5: change style (e.g. photorealistic → cinematic, digital art → oil painting)

For each variation provide:
- "full": complete prompt up to 1000 characters (used for image generation)
- "short": 80-120 character summary (used as text overlay on image)

Return JSON only:
{
  "variants": [
    {"full": "...", "short": "..."},
    ...
  ]
}"""


async def _analyze_reference(image_data: bytes) -> dict:
    """Call 1: OCR + visual description + flag + base_prompt."""
    b64 = base64.b64encode(image_data).decode()
    resp = await client.chat.completions.create(
        model=MODEL_ANALYZER,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": ANALYZE_PROMPT},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ],
            }
        ],
        response_format={"type": "json_object"},
    )
    return json.loads(resp.choices[0].message.content)


async def _generate_variants(base_prompt: str) -> list[dict]:
    """Call 2: generate 5 prompt variants (full + short each)."""
    resp = await client.chat.completions.create(
        model=MODEL_ANALYZER,
        messages=[
            {"role": "user", "content": f"{VARIANTS_PROMPT}\n\nBase prompt:\n{base_prompt}"}
        ],
        response_format={"type": "json_object"},
    )
    data = json.loads(resp.choices[0].message.content)
    return data.get("variants", [])


async def run_analysis(bot, chat_id: int):
    try:
        # Find root refs folder
        refs_folder_id = await drive.get_folder_id(DRIVE_FOLDER_REFS)
        if not refs_folder_id:
            await bot.send_message(chat_id, f"Папка '{DRIVE_FOLDER_REFS}' не найдена на Google Drive.")
            await set_state(analysis_status="idle")
            return

        # List category subfolders
        subfolders = await drive.list_files(refs_folder_id)
        categories = [f for f in subfolders if f["mimeType"] == "application/vnd.google-apps.folder"]

        if not categories:
            await bot.send_message(chat_id, "Нет категорий в папке Референсы.")
            await set_state(analysis_status="idle")
            return

        # Collect all reference files
        all_refs = []
        for cat in categories:
            files = await drive.list_files(cat["id"])
            images = [f for f in files if "image" in f.get("mimeType", "")]
            for img in images:
                all_refs.append({"file": img, "category": cat["name"]})

        total = len(all_refs)
        new_count = 0
        skipped = 0

        # Check which are already processed
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT gdrive_file_id, md5 FROM refs") as cur:
                existing = {row["gdrive_file_id"]: row["md5"] async for row in cur}

        await bot.send_message(
            chat_id,
            f"Найдено {total} референсов. Начинаю проверку..."
        )

        to_process = []
        for ref in all_refs:
            file_id = ref["file"]["id"]
            if file_id not in existing:
                to_process.append(ref)
            else:
                # Download to check MD5
                data = await drive.download_file(file_id)
                md5 = await drive.compute_md5(data)
                if md5 != existing[file_id]:
                    to_process.append({**ref, "_data": data, "_md5": md5})
                else:
                    skipped += 1
                await asyncio.sleep(DELAY_GDRIVE_DOWNLOAD)

        new_count = len(to_process)
        await bot.send_message(
            chat_id,
            f"Новых: {new_count}. Уже обработанных: {skipped}. Начинаю анализ {new_count} новых."
        )

        if not to_process:
            await set_state(analysis_status="done")
            return

        processed = 0
        for ref in to_process:
            try:
                file_id = ref["file"]["id"]
                category = ref["category"]
                data = ref.get("_data") or await drive.download_file(file_id)
                md5 = ref.get("_md5") or await drive.compute_md5(data)

                # GPT-4o call 1: analyze
                analysis = await _analyze_reference(data)
                base_prompt = analysis.get("base_prompt", "")

                # GPT-4o call 2: 5 variants
                variants = await _generate_variants(base_prompt)

                prompts_json = json.dumps(variants, ensure_ascii=False)
                today = date.today().isoformat()

                async with aiosqlite.connect(DB_PATH) as db:
                    # Remove old entry if file changed
                    await db.execute("DELETE FROM refs WHERE gdrive_file_id = ?", (file_id,))
                    await db.execute(
                        """INSERT INTO refs (filename, category, gdrive_file_id, md5, processed_at, prompts)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (ref["file"]["name"], category, file_id, md5, today, prompts_json),
                    )
                    await db.commit()

                processed += 1
                await asyncio.sleep(DELAY_GDRIVE_DOWNLOAD)

            except Exception as e:
                logger.error(f"Error processing {ref['file']['name']}: {e}")

        total_prompts = processed * 5
        weeks = round(total_prompts / IMAGES_PER_WEEK, 1)

        await bot.send_message(
            chat_id,
            f"Готово. Создано {total_prompts} промптов ({processed} референсов × 5).\n"
            f"Распределено по {weeks} неделям.\n\n"
            f"Запустить генерацию для недели 1? → /pinterest_generate 1"
        )
        await set_state(analysis_status="done")

    except Exception as e:
        logger.error(f"Analysis failed: {e}")
        await bot.send_message(chat_id, f"Ошибка анализа: {e}")
        await set_state(analysis_status="idle")
