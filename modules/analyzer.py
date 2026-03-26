import asyncio
import base64
import json
import logging
import re
from datetime import date

import aiosqlite
from openai import AsyncOpenAI

from config import (
    DB_PATH,
    DELAY_GDRIVE_DOWNLOAD,
    DRIVE_BASE_PATH,
    DRIVE_FOLDER_REFS,
    OPENROUTER_API_KEY,
    OPENROUTER_BASE_URL,
    MODEL_ANALYZER,
    GENERATIONS_PER_PROMPT,
)
from database import set_state
from modules import drive

logger = logging.getLogger(__name__)

client = AsyncOpenAI(api_key=OPENROUTER_API_KEY, base_url=OPENROUTER_BASE_URL, timeout=180.0)

# ── Category helpers ──────────────────────────────────────────────────────────

def _is_neurophoto(cat: str) -> bool:
    return "нейрофото" in cat.lower()

def _is_logo(cat: str) -> bool:
    return "логотип" in cat.lower()

def _is_3d_no_prompt(cat: str) -> bool:
    return "без промпта" in cat.lower()

def _is_3d_with_prompt(cat: str) -> bool:
    c = cat.lower()
    return ("3d" in c or "3д" in c) and "без" not in c

def _is_product(cat: str) -> bool:
    return "товар" in cat.lower()

# ── System prompts ─────────────────────────────────────────────────────────────

_PROMPT_OCR = """Your only task: read the generation prompt text printed on this image.

The prompt is usually located at the bottom of the image in a text block.

Rules:
- Copy it word for word, every single sentence. Do NOT shorten, summarize, or rephrase.
- If there is no readable text → describe the image in detail and write a generation prompt yourself.

Return JSON only, no markdown fences:
{"base_prompt": "<exact text from image>"}"""

_PROMPT_ADAPT_NEUROPHOTO = """You are adapting an AI image generation prompt for face-swap / image-to-image portrait generation.

The user's reference photo will be attached to the generation. The user wears glasses.

Your tasks:
1. Remove ALL descriptions of a specific person's appearance: gender words (man, woman, male, female, boy, girl), hair color/style, beard, age, facial features, skin tone, etc.
2. Keep all style, lighting, background, clothing, and art-direction instructions intact.
3. Ensure the prompt references the person from the attached photo. If no such phrase exists, add it naturally (e.g. "of the person from the input photo", "based on the reference portrait").
4. Produce TWO versions:
   - "with_glasses": includes a natural mention of glasses (e.g. "wearing glasses", "with glasses")
   - "no_glasses": identical but without any mention of glasses

The output language must match the input prompt language.
Return JSON only, no markdown:
{"with_glasses": "<prompt with glasses>", "no_glasses": "<prompt without glasses>"}"""

_PROMPT_ADAPT_LOGO = """You are adapting an AI image generation prompt for logo design generation.

The user will attach a specific logo image to the generation request.

Your tasks:
1. Remove any mentions of specific company names, brand names, or specific logos (e.g. "Nike logo", "Apple", "McDonald's golden arches", etc.)
2. Replace those mentions with generic phrases like "the logo from the reference image", "the provided logo", or "this logo".
3. Keep all style, color, material, lighting, and composition instructions completely intact.

The output language must match the input prompt language.
Return JSON only, no markdown:
{"adapted": "<universalized prompt>"}"""

_PROMPT_ANALYZE_3D_IMAGE = """You are analyzing a 3D text effect image to write a highly accurate AI generation prompt.

The image shows stylized 3D text with a specific visual effect. Your task: write a HIGHLY DETAILED prompt (100-200 words) that reproduces this EXACT effect as closely as possible.

Analyze and describe every detail:
1. Letter shape: blocky, slim, inflated, extruded, carved, rounded, sharp-edged, etc.
2. Material & surface: exact type (brushed gold, chrome, glass, plastic, stone, neon, wood, liquid metal, etc.) — be very specific, not generic.
3. Colors: exact hues of the letters, highlights, shadows, reflections.
4. Lighting: direction, intensity, color of light source, specular highlights, shadow softness.
5. Special effects: glow, particles, sparkles, smoke, fire, vines, cracks, gems, etc. — describe exactly what you see.
6. Background: exact color (e.g. "deep navy blue", "pure white", "dark charcoal").

Mandatory requirements to include:
- Use "YOUR TEXT" as the placeholder for the 3D text content — never mention specific letters or words.
- Include: "plain solid-color background"
- Include: "top 20% and bottom 20% of the image must remain empty, no elements"
- Write the prompt in English only.

Return JSON only, no markdown:
{"prompt": "<highly detailed generation prompt>"}"""

_PROMPT_ADAPT_3D_WITH_PROMPT = """You are adapting a 3D text generation prompt.

Your tasks:
1. Replace any specific word, phrase, or text that appears inside the 3D letters with "YOUR TEXT".
2. Add the following technical requirements at the end if they are not already present:
   - "Plain solid-color background."
   - "Top 20% and bottom 20% of the image must remain empty."
3. Keep all style, material, lighting, and effect instructions intact.

The output language must match the input prompt language.
Return JSON only, no markdown:
{"full": "<adapted prompt>"}"""

_PROMPT_ANALYZE_PRODUCT = """Ты анализируешь изображение товара, сгенерированное нейросетью.

Твоя задача — написать ПОДРОБНЫЙ промпт на РУССКОМ языке (100-200 слов), который позволит воспроизвести это изображение максимально точно.

Проанализируй и опиши каждую деталь:
1. Товар: что именно изображено (косметика, еда, электроника, одежда и т.д.), форма, размер, расположение.
2. Материалы и текстуры: поверхность товара, упаковка, этикетки (без конкретных брендов — заменяй на "стильная этикетка", "минималистичный логотип" и т.д.).
3. Окружение и композиция: фон, поверхность, декоративные элементы вокруг товара (цветы, ткани, капли воды, листья и т.д.).
4. Освещение: направление, мягкость, цветовая температура, блики, тени.
5. Цветовая палитра: точные оттенки основных цветов.
6. Стиль съёмки: рекламная, lifestyle, flat lay, макро и т.д.
7. Настроение и атмосфера: премиальность, минимализм, уют, свежесть и т.д.

Правила:
- Пиши на русском языке.
- НЕ упоминай конкретные бренды или названия — описывай визуально.
- Промпт должен быть готов к использованию: один абзац, без нумерации.

Return JSON only, no markdown:
{"prompt": "<подробный промпт на русском>"}"""

# ── Analysis helpers ───────────────────────────────────────────────────────────

async def _ocr(image_data: bytes) -> str:
    """Extract prompt text from image via GPT-4o OCR."""
    b64 = base64.b64encode(image_data).decode()
    resp = await client.chat.completions.create(
        model=MODEL_ANALYZER,
        messages=[{"role": "user", "content": [
            {"type": "text", "text": _PROMPT_OCR},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ]}],
        response_format={"type": "json_object"},
        max_tokens=4000,
    )
    return json.loads(resp.choices[0].message.content).get("base_prompt", "")


async def _adapt_neurophoto(base_prompt: str) -> dict:
    resp = await client.chat.completions.create(
        model=MODEL_ANALYZER,
        messages=[{"role": "user", "content": [
            {"type": "text", "text": _PROMPT_ADAPT_NEUROPHOTO},
            {"type": "text", "text": f"Prompt to adapt:\n{base_prompt}"},
        ]}],
        response_format={"type": "json_object"},
        max_tokens=2000,
    )
    return json.loads(resp.choices[0].message.content)


async def _adapt_logo(base_prompt: str) -> dict:
    resp = await client.chat.completions.create(
        model=MODEL_ANALYZER,
        messages=[{"role": "user", "content": [
            {"type": "text", "text": _PROMPT_ADAPT_LOGO},
            {"type": "text", "text": f"Prompt to adapt:\n{base_prompt}"},
        ]}],
        response_format={"type": "json_object"},
        max_tokens=2000,
    )
    return json.loads(resp.choices[0].message.content)


async def _analyze_3d_image(image_data: bytes) -> dict:
    """GPT-4o analyzes 3D text image and generates a prompt (no text on image)."""
    b64 = base64.b64encode(image_data).decode()
    # First try with json_object mode
    for use_json_mode in (True, False):
        kwargs = dict(
            model=MODEL_ANALYZER,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": _PROMPT_ANALYZE_3D_IMAGE},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ]}],
            max_tokens=2000,
        )
        if use_json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        resp = await client.chat.completions.create(**kwargs)
        content = resp.choices[0].message.content
        if content is None:
            logger.warning("_analyze_3d_image: content is None, retrying without json_object mode")
            continue
        try:
            return json.loads(content)
        except Exception:
            # Extract JSON from plain text response
            match = re.search(r'\{.*\}', content, re.DOTALL)
            if match:
                return json.loads(match.group())
    return {"prompt": ""}


async def _adapt_3d_prompt(base_prompt: str) -> dict:
    resp = await client.chat.completions.create(
        model=MODEL_ANALYZER,
        messages=[{"role": "user", "content": [
            {"type": "text", "text": _PROMPT_ADAPT_3D_WITH_PROMPT},
            {"type": "text", "text": f"Prompt to adapt:\n{base_prompt}"},
        ]}],
        response_format={"type": "json_object"},
        max_tokens=2000,
    )
    return json.loads(resp.choices[0].message.content)


async def _analyze_product_image(image_data: bytes) -> dict:
    """GPT-4o analyzes product image and writes a generation prompt in Russian."""
    b64 = base64.b64encode(image_data).decode()
    for use_json_mode in (True, False):
        kwargs = dict(
            model=MODEL_ANALYZER,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": _PROMPT_ANALYZE_PRODUCT},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ]}],
            max_tokens=2000,
        )
        if use_json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        resp = await client.chat.completions.create(**kwargs)
        content = resp.choices[0].message.content
        if content is None:
            logger.warning("_analyze_product_image: content is None, retrying without json_object mode")
            continue
        try:
            return json.loads(content)
        except Exception:
            match = re.search(r'\{.*\}', content, re.DOTALL)
            if match:
                return json.loads(match.group())
    return {"prompt": ""}


async def _analyze_ref(image_data: bytes, category: str) -> dict:
    """Returns a prompts dict based on the category type."""
    if _is_product(category):
        result = await _analyze_product_image(image_data)
        return {
            "full": result.get("prompt", ""),
        }

    elif _is_neurophoto(category):
        base = await _ocr(image_data)
        adapted = await _adapt_neurophoto(base)
        return {
            "full_glasses": adapted.get("with_glasses", base),
            "full_no_glasses": adapted.get("no_glasses", base),
        }

    elif _is_logo(category):
        base = await _ocr(image_data)
        adapted = await _adapt_logo(base)
        return {
            "full": adapted.get("adapted", base),
        }

    elif _is_3d_no_prompt(category):
        result = await _analyze_3d_image(image_data)
        return {
            "full": result.get("prompt", ""),
            "short": "ТВОЙ ТЕКСТ",
        }

    elif _is_3d_with_prompt(category):
        base = await _ocr(image_data)
        adapted = await _adapt_3d_prompt(base)
        return {
            "full": adapted.get("full", base),
            "short": "ТВОЙ ТЕКСТ",
        }

    else:
        # Fallback: plain OCR
        base = await _ocr(image_data)
        return {"full": base}


# ── Main entry point ───────────────────────────────────────────────────────────

async def run_analysis(bot, chat_id: int):
    try:
        refs_base = f"{DRIVE_BASE_PATH}/{DRIVE_FOLDER_REFS}"

        # List top-level category subfolders
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

        # Collect all image files (two-level: supports sub-folders like "3D Текст/С промптом")
        all_refs = []
        for cat in categories:
            cat_path = f"{refs_base}/{cat['name']}"
            subdirs = await drive.list_dirs(cat_path)

            if subdirs:
                # Category has sub-folders (e.g. "3D Текст" → "С промптом" / "Без промпта")
                for sub in subdirs:
                    sub_path = f"{cat_path}/{sub['name']}"
                    files = await drive.list_files(sub_path)
                    images = [f for f in files if
                              "image" in f.get("mime_type", "") or
                              f["name"].lower().endswith((".jpg", ".jpeg", ".png", ".webp"))]
                    for img in images:
                        img["category"] = f"{cat['name']}/{sub['name']}"
                        img["path"] = f"{sub_path}/{img['name']}"
                    all_refs.extend(images)
            else:
                files = await drive.list_files(cat_path)
                images = [f for f in files if
                          "image" in f.get("mime_type", "") or
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
            await bot.send_message(chat_id, "Все референсы уже обработаны. Можно запускать генерацию.")
            return

        processed = 0
        for ref in to_process:
            try:
                data = await drive.download_file(ref["path"])
                md5 = await drive.compute_md5(data)

                prompt_dict = await _analyze_ref(data, ref["category"])
                prompts_json = json.dumps([prompt_dict], ensure_ascii=False)

                logger.info(f"Analyzed '{ref['name']}' [{ref['category']}]: {str(prompt_dict)[:120]}...")

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

        await bot.send_message(
            chat_id,
            f"Готово. Обработано {processed} референсов.\n"
            f"Генераций: {processed} × {GENERATIONS_PER_PROMPT} = {processed * GENERATIONS_PER_PROMPT} изображений.\n\n"
            f"Запустить генерацию → Pinterest → Генерация неделя 1"
        )
        await set_state(analysis_status="done")

    except Exception as e:
        logger.error(f"Analysis failed: {e}")
        await bot.send_message(chat_id, f"Ошибка анализа: {e}")
        await set_state(analysis_status="idle")
