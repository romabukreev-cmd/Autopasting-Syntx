import asyncio
import base64
import json
import logging
from datetime import date

import aiosqlite
import httpx

from config import (
    DB_PATH,
    DELAY_BETWEEN_GENERATIONS,
    DRIVE_BASE_PATH,
    DRIVE_FOLDER_GENS,
    IMAGES_PER_WEEK,
    MAX_GENERATION_ATTEMPTS,
    MODEL_IMAGE_1,
    MODEL_IMAGE_2,
    OPENROUTER_API_KEY,
    OPENROUTER_BASE_URL,
    RETRY_DELAY,
)
from database import set_state
from modules import drive, overlay

logger = logging.getLogger(__name__)


async def _generate_image(prompt: str, model: str) -> bytes:
    """Call OpenRouter chat/completions with modalities=image to get image bytes."""
    async with httpx.AsyncClient(timeout=120) as http:
        resp = await http.post(
            f"{OPENROUTER_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "modalities": ["image", "text"],
                "image_config": {"aspect_ratio": "9:16"},
            },
        )
    resp.raise_for_status()
    data = resp.json()
    images = data["choices"][0]["message"].get("images", [])
    if not images:
        raise ValueError(f"No images in response. Content: {data['choices'][0]['message'].get('content', '')[:200]}")
    url = images[0]["image_url"]["url"]  # data:image/png;base64,...
    _, b64 = url.split(",", 1)
    return base64.b64decode(b64)


async def _generate_with_retry(prompt: str, model: str) -> bytes | None:
    for attempt in range(MAX_GENERATION_ATTEMPTS):
        try:
            return await _generate_image(prompt, model)
        except Exception as e:
            logger.warning(f"Generation attempt {attempt + 1} failed: {e}")
            if attempt < MAX_GENERATION_ATTEMPTS - 1:
                await asyncio.sleep(RETRY_DELAY)
    return None


def _interleave_prompts(refs: list) -> list[dict]:
    """Round-robin by category so categories alternate."""
    by_category: dict[str, list] = {}
    for ref in refs:
        cat = ref["category"]
        prompts = json.loads(ref["prompts"])
        for i, p in enumerate(prompts):
            by_category.setdefault(cat, []).append({
                "ref_id": ref["id"],
                "category": cat,
                "prompt_index": i,
                "full": p.get("full", ""),
                "short": p.get("short", ""),
            })

    queue = []
    cats = list(by_category.keys())
    max_len = max((len(v) for v in by_category.values()), default=0)
    for i in range(max_len):
        for cat in cats:
            if i < len(by_category[cat]):
                queue.append(by_category[cat][i])
    return queue


async def _get_week_prompts(week: int) -> list[dict]:
    offset = (week - 1) * IMAGES_PER_WEEK

    # Check if week already scheduled
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
                    "SELECT id, category, prompts FROM refs WHERE id = ?", (row["reference_id"],)
                ) as cur2:
                    ref = await cur2.fetchone()
                if ref:
                    prompts = json.loads(ref["prompts"])
                    if row["prompt_index"] < len(prompts):
                        p = prompts[row["prompt_index"]]
                        result.append({
                            "gen_id": row["id"],
                            "ref_id": row["reference_id"],
                            "category": ref["category"],
                            "prompt_index": row["prompt_index"],
                            "full": p.get("full", ""),
                            "short": p.get("short", ""),
                        })
            return result

        # Build week from all refs
        async with db.execute("SELECT id, category, prompts FROM refs ORDER BY id") as cur:
            all_refs = [dict(r) for r in await cur.fetchall()]

    prompt_queue = _interleave_prompts(all_refs)
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


async def _process_one(gen_id: int, item: dict) -> tuple[bool, str, str]:
    """
    Generate one image, apply overlay, upload to Drive.
    Returns (success, clean_file_id, pin_file_id).
    """
    category = item["category"]
    today = date.today().strftime("%Y-%m-%d")
    folder_name = f"{today}_{category}"
    base_path = f"{DRIVE_BASE_PATH}/{DRIVE_FOLDER_GENS}/{folder_name}"
    clean_path = f"{base_path}/clean/gen_{gen_id:04d}.jpg"
    pin_path = f"{base_path}/pinterest/gen_{gen_id:04d}.jpg"

    model = MODEL_IMAGE_1 if gen_id % 2 == 1 else MODEL_IMAGE_2

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE generations SET attempt_count = attempt_count + 1 WHERE id = ?", (gen_id,)
        )
        await db.commit()

    image_data = await _generate_with_retry(item["full"], model)
    if image_data is None:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE generations SET status = 'failed' WHERE id = ?", (gen_id,))
            await db.commit()
        return False, "", ""

    # Upload clean version
    clean_file_id = await drive.upload_file(image_data, clean_path)

    # Create and upload pinterest version
    pin_data = overlay.apply_overlay(image_data, item["short"])
    pin_file_id = await drive.upload_file(pin_data, pin_path)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE generations SET status = 'success', gdrive_file_id = ?, pinterest_file_id = ? WHERE id = ?",
            (clean_file_id, pin_file_id, gen_id),
        )
        await db.commit()

    return True, clean_file_id, pin_file_id


async def run_generation(bot, chat_id: int, week: int):
    try:
        items = await _get_week_prompts(week)
        if not items:
            await bot.send_message(chat_id, f"Нет промптов для недели {week}. Сначала запусти анализ референсов.")
            await set_state(generation_status="idle")
            return

        total = len(items)
        success = 0
        failed = 0
        progress_msg = await bot.send_message(chat_id, f"Генерация: 0/{total}")

        for i, item in enumerate(items):
            gen_id = item.get("gen_id")
            if not gen_id:
                continue

            ok, _, _ = await _process_one(gen_id, item)
            if ok:
                success += 1
            else:
                failed += 1

            if (i + 1) % 5 == 0 or i + 1 == total:
                await progress_msg.edit_text(f"Генерация: {i + 1}/{total}")

            await asyncio.sleep(DELAY_BETWEEN_GENERATIONS)

        status = "done" if failed == 0 else "partial"
        await set_state(generation_status=status)

        text = f"Генерация завершена. Успешно: {success}/{total}. Упало: {failed}."
        if failed > 0:
            text += "\n\nЗапустить повтор → Pinterest → Повторить упавшие"
        else:
            text += "\n\nПроверь изображения на Drive и запускай постинг → Pinterest → Запустить постинг"

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
                "SELECT g.id, g.reference_id, g.prompt_index, r.category, r.prompts "
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
        success = 0

        for i, row in enumerate(failed_rows):
            prompts = json.loads(row["prompts"])
            if row["prompt_index"] >= len(prompts):
                continue
            p = prompts[row["prompt_index"]]
            item = {
                "gen_id": row["id"],
                "category": row["category"],
                "full": p.get("full", ""),
                "short": p.get("short", ""),
            }

            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE generations SET attempt_count = 0, status = 'pending' WHERE id = ?", (row["id"],)
                )
                await db.commit()

            ok, _, _ = await _process_one(row["id"], item)
            if ok:
                success += 1

            if (i + 1) % 5 == 0 or i + 1 == total:
                await progress_msg.edit_text(f"Повтор: {i + 1}/{total}")

            await asyncio.sleep(DELAY_BETWEEN_GENERATIONS)

        await set_state(generation_status="partial" if success < total else "done")
        await bot.send_message(chat_id, f"Повторная генерация завершена. Успешно: {success}/{total}.")

    except Exception as e:
        logger.error(f"Retry failed: {e}")
        await bot.send_message(chat_id, f"Ошибка: {e}")
        await set_state(generation_status="idle")
