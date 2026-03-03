import asyncio
import base64
import json
import logging
from datetime import date

import aiosqlite
from openai import AsyncOpenAI

from config import (
    DB_PATH,
    DELAY_BETWEEN_GENERATIONS,
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

client = AsyncOpenAI(api_key=OPENROUTER_API_KEY, base_url=OPENROUTER_BASE_URL)


async def _generate_image(prompt: str, model: str) -> bytes:
    """Generate one image via OpenRouter. Returns raw image bytes."""
    resp = await client.images.generate(
        model=model,
        prompt=prompt,
        size="832x1248",  # closest to 2:3 for most models
        response_format="b64_json",
        n=1,
    )
    b64 = resp.data[0].b64_json
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


async def _get_week_prompts(week: int) -> list[dict]:
    """Return list of {id, ref_id, prompt_index, full_prompt, short_prompt, category}."""
    offset = (week - 1) * IMAGES_PER_WEEK
    rows = []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, reference_id, prompt_index, week_number, status FROM generations "
            "WHERE week_number = ? AND status = 'pending'",
            (week,),
        ) as cur:
            gen_rows = await cur.fetchall()

        if gen_rows:
            # Week already scheduled → return pending
            for row in gen_rows:
                ref_data = await _get_ref_prompt(db, row["reference_id"], row["prompt_index"])
                if ref_data:
                    rows.append({**ref_data, "gen_id": row["id"]})
            return rows

        # Build week schedule from refs table
        async with db.execute(
            "SELECT id, category, prompts FROM refs ORDER BY id"
        ) as cur:
            all_refs = await cur.fetchall()

    # Flatten all prompts in round-robin by category
    prompt_queue = _interleave_prompts(all_refs)
    week_slice = prompt_queue[offset: offset + IMAGES_PER_WEEK]

    # Write to generations table
    async with aiosqlite.connect(DB_PATH) as db:
        for item in week_slice:
            await db.execute(
                """INSERT OR IGNORE INTO generations
                   (reference_id, prompt_index, week_number, status)
                   VALUES (?, ?, ?, 'pending')""",
                (item["ref_id"], item["prompt_index"], week),
            )
        await db.commit()

    return await _get_week_prompts(week)


def _interleave_prompts(refs) -> list[dict]:
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


async def _get_ref_prompt(db, ref_id: int, prompt_index: int) -> dict | None:
    async with db.execute(
        "SELECT id, category, prompts FROM refs WHERE id = ?", (ref_id,)
    ) as cur:
        ref = await cur.fetchone()
    if not ref:
        return None
    prompts = json.loads(ref["prompts"])
    if prompt_index >= len(prompts):
        return None
    p = prompts[prompt_index]
    return {
        "ref_id": ref_id,
        "category": ref["category"],
        "prompt_index": prompt_index,
        "full": p.get("full", ""),
        "short": p.get("short", ""),
    }


async def _process_generation(gen_id: int, item: dict, gens_folder_id: str, bot, chat_id: int, progress_msg):
    """Generate one image, overlay, upload to Drive. Updates DB."""
    category = item["category"]
    full_prompt = item["full"]
    short_prompt = item["short"]

    # Alternate models: odd indices → model 1, even → model 2
    model = MODEL_IMAGE_1 if (gen_id % 2 == 1) else MODEL_IMAGE_2

    # Update attempt count
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE generations SET attempt_count = attempt_count + 1 WHERE id = ?",
            (gen_id,),
        )
        await db.commit()

    image_data = await _generate_with_retry(full_prompt, model)
    if image_data is None:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE generations SET status = 'failed' WHERE id = ?", (gen_id,)
            )
            await db.commit()
        return False

    today = date.today().strftime("%Y-%m-%d")
    folder_name = f"{today}_{category}"

    # Get or create category folder
    cat_folder_id = await drive.get_or_create_folder(folder_name, gens_folder_id)
    clean_folder_id = await drive.get_or_create_folder("clean", cat_folder_id)
    pinterest_folder_id = await drive.get_or_create_folder("pinterest", cat_folder_id)

    filename = f"gen_{gen_id:04d}.jpg"

    # Upload clean version
    clean_file_id = await drive.upload_file(filename, "image/jpeg", image_data, clean_folder_id)

    # Create and upload pinterest version
    pinterest_data = overlay.apply_overlay(image_data, short_prompt)
    pin_file_id = await drive.upload_file(filename, "image/jpeg", pinterest_data, pinterest_folder_id)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE generations
               SET status = 'success', gdrive_file_id = ?, pinterest_file_id = ?
               WHERE id = ?""",
            (clean_file_id, pin_file_id, gen_id),
        )
        await db.commit()

    return True


async def run_generation(bot, chat_id: int, week: int):
    try:
        gens_folder_id = await drive.get_or_create_folder("База генераций")

        items = await _get_week_prompts(week)
        if not items:
            await bot.send_message(chat_id, f"Нет промптов для недели {week}.")
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

            ok = await _process_generation(gen_id, item, gens_folder_id, bot, chat_id, progress_msg)
            if ok:
                success += 1
            else:
                failed += 1

            if (i + 1) % 5 == 0 or i + 1 == total:
                await progress_msg.edit_text(f"Генерация: {i + 1}/{total}")

            await asyncio.sleep(DELAY_BETWEEN_GENERATIONS)

        status = "done" if failed == 0 else "partial"
        await set_state(generation_status=status)

        result_text = (
            f"Генерация завершена. Успешно: {success}/{total}. Упало: {failed}."
        )
        if failed > 0:
            result_text += "\n\nЗапустить повторную генерацию для упавших? → /pinterest_retry"
        else:
            result_text += "\n\nПроверь изображения и запускай постинг → /pinterest_start"

        await bot.send_message(chat_id, result_text)

    except Exception as e:
        logger.error(f"Generation failed: {e}")
        await bot.send_message(chat_id, f"Ошибка генерации: {e}")
        await set_state(generation_status="idle")


async def run_retry(bot, chat_id: int):
    try:
        gens_folder_id = await drive.get_or_create_folder("База генераций")

        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id, reference_id, prompt_index FROM generations WHERE status = 'failed'"
            ) as cur:
                failed_rows = await cur.fetchall()

        if not failed_rows:
            await bot.send_message(chat_id, "Нет упавших генераций.")
            await set_state(generation_status="idle")
            return

        total = len(failed_rows)
        progress_msg = await bot.send_message(chat_id, f"Повтор: 0/{total}")
        success = 0

        for i, row in enumerate(failed_rows):
            async with aiosqlite.connect(DB_PATH) as db:
                item = await _get_ref_prompt(db, row["reference_id"], row["prompt_index"])

            if item:
                # Reset attempt count for retry
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute(
                        "UPDATE generations SET attempt_count = 0, status = 'pending' WHERE id = ?",
                        (row["id"],),
                    )
                    await db.commit()

                item["gen_id"] = row["id"]
                ok = await _process_generation(row["id"], item, gens_folder_id, bot, chat_id, progress_msg)
                if ok:
                    success += 1

            if (i + 1) % 5 == 0 or i + 1 == total:
                await progress_msg.edit_text(f"Повтор: {i + 1}/{total}")

            await asyncio.sleep(DELAY_BETWEEN_GENERATIONS)

        await set_state(generation_status="partial" if success < total else "done")
        await bot.send_message(
            chat_id,
            f"Повторная генерация завершена. Успешно: {success}/{total}."
        )

    except Exception as e:
        logger.error(f"Retry failed: {e}")
        await bot.send_message(chat_id, f"Ошибка: {e}")
        await set_state(generation_status="idle")
