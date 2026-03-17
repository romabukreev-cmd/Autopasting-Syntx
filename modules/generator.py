import asyncio
import base64
import json
import logging
import random
import re
from datetime import date

import aiosqlite
import httpx

from config import (
    DB_PATH,
    DELAY_BETWEEN_GENERATIONS,
    DRIVE_BASE_PATH,
    DRIVE_FOLDER_GENS,
    DRIVE_FOLDER_LOGOS,
    DRIVE_FOLDER_REFS,
    DRIVE_FOLDER_USER_PHOTOS,
    GENERATIONS_PER_PROMPT,
    IMAGES_PER_DAY_MIN,
    IMAGES_PER_DAY_MAX,
    IMAGES_PER_WEEK,
    MAX_GENERATION_ATTEMPTS,
    MODEL_IMAGE,
    OPENROUTER_API_KEY,
    OPENROUTER_BASE_URL,
    RETRY_DELAY,
)
from database import set_state
from modules import drive, overlay, sheets

logger = logging.getLogger(__name__)


def _is_image_only_model(model: str) -> bool:
    """Image-only models use modalities=["image"] (no text output)."""
    image_only_prefixes = ("bytedance-seed/", "stability", "black-forest-labs/", "recraft-ai/")
    return any(model.startswith(p) for p in image_only_prefixes)


def _is_neurophoto(cat: str) -> bool:
    return "нейрофото" in cat.lower()


def _is_logo(cat: str) -> bool:
    return "логотип" in cat.lower()


def _is_3d_text(cat: str) -> bool:
    c = cat.lower()
    return "3d" in c or "3д" in c


async def _fetch_random_images(folder_path: str, n: int) -> list[bytes]:
    """Download n unique random images from a Drive folder. Returns up to n items."""
    try:
        files = await drive.list_files(folder_path)
    except Exception as e:
        logger.warning(f"Could not list {folder_path}: {e}")
        return []
    images = [f for f in files if f["name"].lower().endswith((".jpg", ".jpeg", ".png", ".webp"))]
    if not images:
        return []
    random.shuffle(images)
    selected = images[:n]
    result = []
    for f in selected:
        try:
            data = await drive.download_file(f"{folder_path}/{f['name']}")
            result.append(data)
        except Exception as e:
            logger.warning(f"Failed to download {f['name']} from {folder_path}: {e}")
    return result


async def _pick_3d_phrase() -> str:
    """Return a random phrase from the Sheets '3D Фразы' list. Falls back to 'YOUR TEXT'."""
    phrases = sheets.get_phrases()
    if not phrases:
        try:
            phrases = await sheets.load_phrases()
        except Exception as e:
            logger.warning(f"Could not load 3D phrases: {e}")
    return random.choice(phrases) if phrases else "YOUR TEXT"


async def _generate_image(prompt: str, model: str, ref_image: bytes | None = None, aspect_ratio: str = "2:3") -> bytes:
    """Call OpenRouter chat/completions with modalities=image to get image bytes.
    If ref_image is provided, it is passed as the first content part (image-to-image).
    """
    modalities = ["image"] if _is_image_only_model(model) else ["image", "text"]

    if ref_image:
        b64_ref = base64.b64encode(ref_image).decode()
        content = [
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_ref}"}},
            {"type": "text", "text": prompt},
        ]
    else:
        content = prompt

    async with httpx.AsyncClient(timeout=300) as http:
        resp = await http.post(
            f"{OPENROUTER_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": content}],
                "modalities": modalities,
                "image_config": {"aspect_ratio": aspect_ratio},
            },
        )
    resp.raise_for_status()
    data = resp.json()
    msg = data["choices"][0]["message"]

    images = msg.get("images", [])
    if not images:
        raise ValueError(f"No images in response. Content: {str(msg.get('content', ''))[:300]}")
    url = images[0]["image_url"]["url"]
    _, b64 = url.split(",", 1)
    return base64.b64decode(b64)


async def _generate_with_retry(prompt: str, model: str, ref_image: bytes | None = None, aspect_ratio: str = "2:3") -> bytes | None:
    for attempt in range(MAX_GENERATION_ATTEMPTS):
        try:
            return await _generate_image(prompt, model, ref_image, aspect_ratio)
        except Exception as e:
            logger.warning(f"Generation attempt {attempt + 1} failed: {e}")
            if attempt < MAX_GENERATION_ATTEMPTS - 1:
                await asyncio.sleep(RETRY_DELAY)
    return None


def _item_from_prompt(ref_id: int, category: str, ref_filename: str, prompt_index: int, p: dict) -> dict:
    return {
        "ref_id": ref_id,
        "category": category,
        "ref_filename": ref_filename,
        "prompt_index": prompt_index,
        "full": p.get("full", ""),
        "full_glasses": p.get("full_glasses", ""),
        "full_no_glasses": p.get("full_no_glasses", ""),
        "short": p.get("short", ""),
    }


def _build_prompt_queue(refs: list) -> list[dict]:
    """Linear order — 1 prompt per ref, no interleaving."""
    queue = []
    for ref in refs:
        prompts = json.loads(ref["prompts"])
        for i, p in enumerate(prompts):
            queue.append(_item_from_prompt(ref["id"], ref["category"], ref["filename"], i, p))
    return queue


async def _get_week_prompts(week: int) -> list[dict]:
    offset = (week - 1) * IMAGES_PER_WEEK

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT g.id, g.reference_id, g.prompt_index FROM generations g "
            "WHERE g.week_number = ? AND g.status = 'pending'",
            (week,),
        ) as cur:
            existing = await cur.fetchall()

        if existing:
            result = []
            for row in existing:
                async with db.execute(
                    "SELECT id, category, filename, prompts FROM refs WHERE id = ?", (row["reference_id"],)
                ) as cur2:
                    ref = await cur2.fetchone()
                if ref:
                    prompts = json.loads(ref["prompts"])
                    if row["prompt_index"] < len(prompts):
                        p = prompts[row["prompt_index"]]
                        item = _item_from_prompt(
                            row["reference_id"], ref["category"], ref["filename"], row["prompt_index"], p
                        )
                        item["gen_id"] = row["id"]
                        result.append(item)
            return result

        async with db.execute("SELECT id, category, filename, prompts FROM refs ORDER BY id") as cur:
            all_refs = [dict(r) for r in await cur.fetchall()]

    prompt_queue = _build_prompt_queue(all_refs)
    week_slice = prompt_queue[offset: offset + IMAGES_PER_WEEK]

    if not week_slice:
        return []

    async with aiosqlite.connect(DB_PATH) as db:
        for item in week_slice:
            await db.execute(
                "INSERT OR IGNORE INTO generations (reference_id, prompt_index, week_number, status) "
                "VALUES (?, ?, ?, 'pending')",
                (item["ref_id"], item["prompt_index"], week),
            )
        await db.commit()

    return await _get_week_prompts(week)


async def _process_one(gen_id: int, item: dict, week: int) -> bool:
    """
    Generate GENERATIONS_PER_PROMPT images, apply overlay, upload to Drive.
    All files recorded in generation_files table.
    Structure: База генераций / week_{week} / {category} / nanobana | nanobana_pin /

    Format logic:
    - Нейрофото: i2i with user photo (different per gen), full_glasses for gen, full_no_glasses for overlay
    - Логотипы: i2i with logo (different per gen), full for gen and overlay
    - 3D Текст: text-only, "ТВОЙ ТЕКСТ" on overlay
    - Others: text-only
    """
    ref_id = item["ref_id"]
    category = item["category"]
    base_path = f"{DRIVE_BASE_PATH}/{DRIVE_FOLDER_GENS}/week_{week}/{category}"

    # Determine prompt for generation, text for overlay, and aspect ratio
    aspect_ratio = "2:3"
    if _is_neurophoto(category):
        gen_prompt = item.get("full_glasses") or item.get("full", "")
        overlay_text = item.get("full_no_glasses") or item.get("full", "")
    elif _is_3d_text(category):
        aspect_ratio = "3:4"
        raw_prompt = item.get("full", "")
        # Substitute a random phrase from Sheets instead of "YOUR TEXT"
        phrase = await _pick_3d_phrase()
        adapted = re.sub(r"YOUR\s+TEXT", phrase, raw_prompt, flags=re.IGNORECASE)
        if adapted == raw_prompt:
            # YOUR TEXT not found — prepend the phrase
            gen_prompt = f"{phrase}. {raw_prompt}"
            logger.warning(f"gen_{gen_id:04d} 3D prompt had no YOUR TEXT placeholder, prepended phrase")
        else:
            gen_prompt = adapted
        # Overlay shows the prompt with "ТВОЙ ТЕКСТ" placeholder (not the actual phrase)
        overlay_text = re.sub(r"YOUR\s+TEXT", "ТВОЙ ТЕКСТ", raw_prompt, flags=re.IGNORECASE)
        if overlay_text == raw_prompt:
            overlay_text = f"ТВОЙ ТЕКСТ. {raw_prompt[:80]}"
    else:
        gen_prompt = item.get("full", "")
        overlay_text = item.get("full", "")

    logger.info(f"gen_{gen_id:04d} [{category}] prompt: {gen_prompt[:100]}")

    # Pre-fetch reference images for i2i categories
    ref_images: list[bytes | None] = [None] * GENERATIONS_PER_PROMPT
    if _is_neurophoto(category):
        photos_path = f"{DRIVE_BASE_PATH}/{DRIVE_FOLDER_USER_PHOTOS}"
        photos = await _fetch_random_images(photos_path, GENERATIONS_PER_PROMPT)
        for i, p in enumerate(photos):
            ref_images[i] = p
        if not photos:
            logger.warning(f"gen_{gen_id:04d} No user photos found in {photos_path}, generating text-only")
    elif _is_logo(category):
        logos_path = f"{DRIVE_BASE_PATH}/{DRIVE_FOLDER_LOGOS}"
        logos = await _fetch_random_images(logos_path, GENERATIONS_PER_PROMPT)
        for i, lg in enumerate(logos):
            ref_images[i] = lg
        if not logos:
            logger.warning(f"gen_{gen_id:04d} No logos found in {logos_path}, generating text-only")
    elif _is_3d_text(category) and "без промпта" in category.lower():
        # For 3D without prompt: pass the reference image as style guide (i2i)
        ref_path = f"{DRIVE_BASE_PATH}/{DRIVE_FOLDER_REFS}/{category}/{item['ref_filename']}"
        try:
            ref_data = await drive.download_file(ref_path)
            ref_images = [ref_data] * GENERATIONS_PER_PROMPT
            logger.info(f"gen_{gen_id:04d} 3D без промпта: loaded ref for i2i style guidance")
        except Exception as e:
            logger.warning(f"gen_{gen_id:04d} could not load 3D ref for i2i: {e}")

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE generations SET attempt_count = attempt_count + 1 WHERE id = ?", (gen_id,)
        )
        await db.commit()

    ok = 0

    async def _save_file(data: bytes, path: str, model: str, ftype: str):
        file_id = await drive.upload_file(data, path)
        if ftype == "pin":
            await drive.make_public(path)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO generation_files (generation_id, ref_id, model, type, gdrive_file_id, filename) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (gen_id, ref_id, model, ftype, file_id, path),
            )
            await db.commit()
        return file_id

    # --- NanaBanana: GENERATIONS_PER_PROMPT images ---
    for n in range(GENERATIONS_PER_PROMPT):
        ref_img = ref_images[n]
        nb_data = await _generate_with_retry(gen_prompt, MODEL_IMAGE, ref_img, aspect_ratio)
        if nb_data:
            fname = f"gen_{gen_id:04d}_{n+1}.jpg"
            clean_path = f"{base_path}/nanobana/{fname}"
            pin_path = f"{base_path}/nanobana_pin/{fname}"
            await _save_file(nb_data, clean_path, "nanobana", "clean")
            nb_pin = overlay.apply_overlay(nb_data, overlay_text, "nanobana")
            await _save_file(nb_pin, pin_path, "nanobana", "pin")
            ok += 1
            logger.info(f"gen_{gen_id:04d} NanaBana {n+1}/{GENERATIONS_PER_PROMPT}: ok")
        else:
            logger.warning(f"gen_{gen_id:04d} NanaBana {n+1}/{GENERATIONS_PER_PROMPT}: failed")
        await asyncio.sleep(DELAY_BETWEEN_GENERATIONS)

    async with aiosqlite.connect(DB_PATH) as db:
        status = "success" if ok > 0 else "failed"
        await db.execute("UPDATE generations SET status = ? WHERE id = ?", (status, gen_id))
        await db.commit()

    return ok > 0


async def run_generation(bot, chat_id: int, week: int):
    try:
        items = await _get_week_prompts(week)
        if not items:
            await bot.send_message(chat_id, f"Нет промптов для недели {week}. Сначала запусти анализ референсов.")
            await set_state(generation_status="idle")
            return

        total = len(items)
        nb_ok = 0
        failed = 0
        progress_msg = await bot.send_message(chat_id, f"Генерация: 0/{total}")

        for i, item in enumerate(items):
            gen_id = item.get("gen_id")
            if not gen_id:
                continue

            ok = await _process_one(gen_id, item, week)
            if ok:
                nb_ok += 1
            else:
                failed += 1

            if (i + 1) % 1 == 0 or i + 1 == total:
                await progress_msg.edit_text(
                    f"Генерация: {i + 1}/{total} | NanaBana: {nb_ok * GENERATIONS_PER_PROMPT}"
                )

            await asyncio.sleep(DELAY_BETWEEN_GENERATIONS)

        status = "done" if failed == 0 else "partial"
        await set_state(generation_status=status)

        total_images = nb_ok * GENERATIONS_PER_PROMPT
        avg_per_day = (IMAGES_PER_DAY_MIN + IMAGES_PER_DAY_MAX) / 2
        days = total_images / avg_per_day if avg_per_day else 0
        weeks = days / 7
        text = (
            f"Генерация завершена.\n"
            f"NanaBana: {nb_ok * GENERATIONS_PER_PROMPT}/{total * GENERATIONS_PER_PROMPT} ✓\n"
            f"Итого изображений: {total_images}\n"
            f"Хватит на: ~{days:.1f} дн. / {weeks:.1f} нед. ({IMAGES_PER_DAY_MIN}-{IMAGES_PER_DAY_MAX} пинов/день)\n"
            f"Упало полностью: {failed} рефов."
        )
        if failed > 0:
            text += "\n\nЗапустить повтор → Pinterest → Повторить упавшие"
        else:
            text += "\n\nЗапустить постинг → Pinterest → Запустить постинг"

        await bot.send_message(chat_id, text)

    except Exception as e:
        logger.error(f"Generation failed: {e}")
        await bot.send_message(chat_id, f"Ошибка генерации: {e}")
        await set_state(generation_status="idle")


async def run_retry(bot, chat_id: int):
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT g.id, g.reference_id, g.prompt_index, g.week_number, r.category, r.filename, r.prompts "
                "FROM generations g JOIN refs r ON g.reference_id = r.id "
                "WHERE g.status = 'failed'"
            ) as cur:
                failed_rows = [dict(r) for r in await cur.fetchall()]

        if not failed_rows:
            await bot.send_message(chat_id, "Нет упавших генераций.")
            await set_state(generation_status="idle")
            return

        total = len(failed_rows)
        progress_msg = await bot.send_message(chat_id, f"Повтор: 0/{total}")
        nb_ok = 0

        for i, row in enumerate(failed_rows):
            prompts = json.loads(row["prompts"])
            if row["prompt_index"] >= len(prompts):
                continue
            p = prompts[row["prompt_index"]]
            item = _item_from_prompt(
                row["reference_id"], row["category"], row["filename"], row["prompt_index"], p
            )
            item["gen_id"] = row["id"]

            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE generations SET attempt_count = 0, status = 'pending' WHERE id = ?", (row["id"],)
                )
                await db.commit()

            ok = await _process_one(row["id"], item, row["week_number"])
            if ok:
                nb_ok += 1

            if (i + 1) % 1 == 0 or i + 1 == total:
                await progress_msg.edit_text(
                    f"Повтор: {i + 1}/{total} | NanaBana: {nb_ok}"
                )

            await asyncio.sleep(DELAY_BETWEEN_GENERATIONS)

        await set_state(generation_status="partial" if nb_ok < total else "done")
        await bot.send_message(
            chat_id,
            f"Повторная генерация завершена.\nNanaBana: {nb_ok}/{total} ✓"
        )

    except Exception as e:
        logger.error(f"Retry failed: {e}")
        await bot.send_message(chat_id, f"Ошибка: {e}")
        await set_state(generation_status="idle")
