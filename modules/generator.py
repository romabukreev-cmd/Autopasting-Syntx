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
                "image_config": {"aspect_ratio": "2:3"},
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


async def _process_one(gen_id: int, item: dict) -> tuple[bool, bool]:
    """
    Generate image with BOTH models (SeeDream and NanaBana), apply overlay, upload to Drive.
    Saves to seedream/ and nanobana/ subfolders.
    Returns (success_model1, success_model2).
    """
    category = item["category"]
    today = date.today().strftime("%Y-%m-%d")
    folder_name = f"{today}_{category}"
    base_path = f"{DRIVE_BASE_PATH}/{DRIVE_FOLDER_GENS}/{folder_name}"

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE generations SET attempt_count = attempt_count + 1 WHERE id = ?", (gen_id,)
        )
        await db.commit()

    success_gpt = False
    success_nb = False
    main_file_id = ""
    main_pin_id = ""

    # --- SeeDream image ---
    gpt_data = await _generate_with_retry(item["full"], MODEL_IMAGE_1)
    if gpt_data:
        gpt_file_id = await drive.upload_file(gpt_data, f"{base_path}/seedream/gen_{gen_id:04d}.jpg")
        gpt_pin = overlay.apply_overlay(gpt_data, item["short"])
        gpt_pin_id = await drive.upload_file(gpt_pin, f"{base_path}/seedream_pin/gen_{gen_id:04d}.jpg")
        success_gpt = True
        main_file_id = gpt_file_id
        main_pin_id = gpt_pin_id
        logger.info(f"gen_{gen_id:04d} SeeDream: ok")
    else:
        logger.warning(f"gen_{gen_id:04d} SeeDream: failed")

    # --- Nano Banana image ---
    nb_data = await _generate_with_retry(item["full"], MODEL_IMAGE_2)
    if nb_data:
        nb_file_id = await drive.upload_file(nb_data, f"{base_path}/nanobana/gen_{gen_id:04d}.jpg")
        nb_pin = overlay.apply_overlay(nb_data, item["short"])
        await drive.upload_file(nb_pin, f"{base_path}/nanobana_pin/gen_{gen_id:04d}.jpg")
        success_nb = True
        if not main_file_id:
            main_file_id = nb_file_id
        logger.info(f"gen_{gen_id:04d} NanaBana: ok")
    else:
        logger.warning(f"gen_{gen_id:04d} NanaBana: failed")

    # Update DB
    async with aiosqlite.connect(DB_PATH) as db:
        if success_gpt or success_nb:
            await db.execute(
                "UPDATE generations SET status = 'success', gdrive_file_id = ?, pinterest_file_id = ? WHERE id = ?",
                (main_file_id, main_pin_id, gen_id),
            )
        else:
            await db.execute("UPDATE generations SET status = 'failed' WHERE id = ?", (gen_id,))
        await db.commit()

    return success_gpt, success_nb


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

        gpt_ok = 0
        nb_ok = 0
        failed = 0

        for i, item in enumerate(items):
            gen_id = item.get("gen_id")
            if not gen_id:
                continue

            ok_gpt, ok_nb = await _process_one(gen_id, item)
            if ok_gpt:
                gpt_ok += 1
            if ok_nb:
                nb_ok += 1
            if not ok_gpt and not ok_nb:
                failed += 1

            if (i + 1) % 5 == 0 or i + 1 == total:
                await progress_msg.edit_text(
                    f"Генерация: {i + 1}/{total} | SeeDream: {gpt_ok} | NanaBana: {nb_ok}"
                )

            await asyncio.sleep(DELAY_BETWEEN_GENERATIONS)

        status = "done" if failed == 0 else "partial"
        await set_state(generation_status=status)

        text = (
            f"Генерация завершена.\n"
            f"SeeDream: {gpt_ok}/{total} ✓\n"
            f"NanaBana: {nb_ok}/{total} ✓\n"
            f"Упало полностью: {failed}."
        )
        if failed > 0:
            text += "\n\nЗапустить повтор → Pinterest → Повторить упавшие"
        else:
            text += "\n\nПроверь папки seedream/ и nanobana/ на Drive и запускай постинг"

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
        gpt_ok = 0
        nb_ok = 0

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

            ok_gpt, ok_nb = await _process_one(row["id"], item)
            if ok_gpt:
                gpt_ok += 1
            if ok_nb:
                nb_ok += 1

            if (i + 1) % 5 == 0 or i + 1 == total:
                await progress_msg.edit_text(
                    f"Повтор: {i + 1}/{total} | SeeDream: {gpt_ok} | NanaBana: {nb_ok}"
                )

            await asyncio.sleep(DELAY_BETWEEN_GENERATIONS)

        any_success = gpt_ok + nb_ok
        await set_state(generation_status="partial" if any_success < total * 2 else "done")
        await bot.send_message(
            chat_id,
            f"Повторная генерация завершена.\nSeeDream: {gpt_ok}/{total} ✓\nNanaBana: {nb_ok}/{total} ✓"
        )

    except Exception as e:
        logger.error(f"Retry failed: {e}")
        await bot.send_message(chat_id, f"Ошибка: {e}")
        await set_state(generation_status="idle")
