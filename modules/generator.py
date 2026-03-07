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
    GENERATIONS_PER_PROMPT,
    IMAGES_PER_DAY_MIN,
    IMAGES_PER_DAY_MAX,
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


def _is_image_only_model(model: str) -> bool:
    """Image-only models use modalities=["image"] (no text output)."""
    image_only_prefixes = ("bytedance-seed/", "stability", "black-forest-labs/", "recraft-ai/")
    return any(model.startswith(p) for p in image_only_prefixes)


async def _generate_image(prompt: str, model: str) -> bytes:
    """Call OpenRouter chat/completions with modalities=image to get image bytes."""
    modalities = ["image"] if _is_image_only_model(model) else ["image", "text"]

    async with httpx.AsyncClient(timeout=300) as http:
        resp = await http.post(
            f"{OPENROUTER_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "modalities": modalities,
                "image_config": {"aspect_ratio": "2:3"},
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


async def _generate_with_retry(prompt: str, model: str) -> bytes | None:
    for attempt in range(MAX_GENERATION_ATTEMPTS):
        try:
            return await _generate_image(prompt, model)
        except Exception as e:
            logger.warning(f"Generation attempt {attempt + 1} failed: {e}")
            if attempt < MAX_GENERATION_ATTEMPTS - 1:
                await asyncio.sleep(RETRY_DELAY)
    return None


def _build_prompt_queue(refs: list) -> list[dict]:
    """Linear order — 1 prompt per ref, no interleaving."""
    queue = []
    for ref in refs:
        prompts = json.loads(ref["prompts"])
        for i, p in enumerate(prompts):
            queue.append({
                "ref_id": ref["id"],
                "category": ref["category"],
                "prompt_index": i,
                "full": p.get("full", ""),
                "short": p.get("short", ""),
            })
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

        async with db.execute("SELECT id, category, prompts FROM refs ORDER BY id") as cur:
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


async def _process_one(gen_id: int, item: dict, week: int) -> tuple[bool, bool]:
    """
    Generate GENERATIONS_PER_PROMPT images with each model, apply overlay, upload to Drive.
    All files recorded in generation_files table.
    Structure: База генераций / week_{week} / {category} / seedream|nanobana|*_pin /
    """
    ref_id = item["ref_id"]
    category = item["category"]
    base_path = f"{DRIVE_BASE_PATH}/{DRIVE_FOLDER_GENS}/week_{week}/{category}"

    logger.info(f"gen_{gen_id:04d} ref_id={ref_id} prompt: {item['full']}")
    logger.info(f"gen_{gen_id:04d} short: {item['short']}")

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE generations SET attempt_count = attempt_count + 1 WHERE id = ?", (gen_id,)
        )
        await db.commit()

    sd_ok = 0
    nb_ok = 0

    async def _save_file(data: bytes, path: str, model: str, ftype: str, fname: str):
        """Upload file and record in generation_files."""
        file_id = await drive.upload_file(data, path)
        if ftype == "pin":
            await drive.make_public(path)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO generation_files (generation_id, ref_id, model, type, gdrive_file_id, filename) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (gen_id, ref_id, model, ftype, file_id, fname),
            )
            await db.commit()
        return file_id

    # --- SeeDream: GENERATIONS_PER_PROMPT images ---
    for n in range(GENERATIONS_PER_PROMPT):
        sd_data = await _generate_with_retry(item["full"], MODEL_IMAGE_1)
        if sd_data:
            fname = f"gen_{gen_id:04d}_{n+1}.jpg"
            clean_path = f"{base_path}/seedream/{fname}"
            pin_path = f"{base_path}/seedream_pin/{fname}"
            await _save_file(sd_data, clean_path, "seedream", "clean", clean_path)
            sd_pin = overlay.apply_overlay(sd_data, item["full"], "seedream")
            await _save_file(sd_pin, pin_path, "seedream", "pin", pin_path)
            sd_ok += 1
            logger.info(f"gen_{gen_id:04d} SeeDream {n+1}/{GENERATIONS_PER_PROMPT}: ok")
        else:
            logger.warning(f"gen_{gen_id:04d} SeeDream {n+1}/{GENERATIONS_PER_PROMPT}: failed")
        await asyncio.sleep(DELAY_BETWEEN_GENERATIONS)

    # --- NanaBana: GENERATIONS_PER_PROMPT images ---
    for n in range(GENERATIONS_PER_PROMPT):
        nb_data = await _generate_with_retry(item["full"], MODEL_IMAGE_2)
        if nb_data:
            fname = f"gen_{gen_id:04d}_{n+1}.jpg"
            clean_path = f"{base_path}/nanobana/{fname}"
            pin_path = f"{base_path}/nanobana_pin/{fname}"
            await _save_file(nb_data, clean_path, "nanobana", "clean", clean_path)
            nb_pin = overlay.apply_overlay(nb_data, item["full"], "nanobana")
            await _save_file(nb_pin, pin_path, "nanobana", "pin", pin_path)
            nb_ok += 1
            logger.info(f"gen_{gen_id:04d} NanaBana {n+1}/{GENERATIONS_PER_PROMPT}: ok")
        else:
            logger.warning(f"gen_{gen_id:04d} NanaBana {n+1}/{GENERATIONS_PER_PROMPT}: failed")
        await asyncio.sleep(DELAY_BETWEEN_GENERATIONS)

    async with aiosqlite.connect(DB_PATH) as db:
        status = "success" if (sd_ok > 0 or nb_ok > 0) else "failed"
        await db.execute("UPDATE generations SET status = ? WHERE id = ?", (status, gen_id))
        await db.commit()

    return sd_ok > 0, nb_ok > 0


async def run_generation(bot, chat_id: int, week: int):
    try:
        items = await _get_week_prompts(week)
        if not items:
            await bot.send_message(chat_id, f"Нет промптов для недели {week}. Сначала запусти анализ референсов.")
            await set_state(generation_status="idle")
            return

        total = len(items)
        sd_ok = 0
        nb_ok = 0
        failed = 0
        progress_msg = await bot.send_message(chat_id, f"Генерация: 0/{total}")

        for i, item in enumerate(items):
            gen_id = item.get("gen_id")
            if not gen_id:
                continue

            ok_sd, ok_nb = await _process_one(gen_id, item, week)
            if ok_sd:
                sd_ok += 1
            if ok_nb:
                nb_ok += 1
            if not ok_sd and not ok_nb:
                failed += 1

            if (i + 1) % 1 == 0 or i + 1 == total:
                await progress_msg.edit_text(
                    f"Генерация: {i + 1}/{total} | SeeDream: {sd_ok * GENERATIONS_PER_PROMPT} | NanaBana: {nb_ok * GENERATIONS_PER_PROMPT}"
                )

            await asyncio.sleep(DELAY_BETWEEN_GENERATIONS)

        status = "done" if failed == 0 else "partial"
        await set_state(generation_status=status)

        total_images = (sd_ok + nb_ok) * GENERATIONS_PER_PROMPT
        avg_per_day = (IMAGES_PER_DAY_MIN + IMAGES_PER_DAY_MAX) / 2
        days = total_images / avg_per_day if avg_per_day else 0
        weeks = days / 7
        text = (
            f"Генерация завершена.\n"
            f"SeeDream: {sd_ok * GENERATIONS_PER_PROMPT}/{total * GENERATIONS_PER_PROMPT} ✓\n"
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
                "SELECT g.id, g.reference_id, g.prompt_index, g.week_number, r.category, r.prompts "
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
        sd_ok = 0
        nb_ok = 0

        for i, row in enumerate(failed_rows):
            prompts = json.loads(row["prompts"])
            if row["prompt_index"] >= len(prompts):
                continue
            p = prompts[row["prompt_index"]]
            item = {
                "gen_id": row["id"],
                "ref_id": row["reference_id"],
                "category": row["category"],
                "full": p.get("full", ""),
                "short": p.get("short", ""),
            }

            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE generations SET attempt_count = 0, status = 'pending' WHERE id = ?", (row["id"],)
                )
                await db.commit()

            ok_sd, ok_nb = await _process_one(row["id"], item, row["week_number"])
            if ok_sd:
                sd_ok += 1
            if ok_nb:
                nb_ok += 1

            if (i + 1) % 1 == 0 or i + 1 == total:
                await progress_msg.edit_text(
                    f"Повтор: {i + 1}/{total} | SeeDream: {sd_ok} | NanaBana: {nb_ok}"
                )

            await asyncio.sleep(DELAY_BETWEEN_GENERATIONS)

        any_success = sd_ok + nb_ok
        await set_state(generation_status="partial" if any_success < total * 2 else "done")
        await bot.send_message(
            chat_id,
            f"Повторная генерация завершена.\nSeeDream: {sd_ok}/{total} ✓\nNanaBana: {nb_ok}/{total} ✓"
        )

    except Exception as e:
        logger.error(f"Retry failed: {e}")
        await bot.send_message(chat_id, f"Ошибка: {e}")
        await set_state(generation_status="idle")
