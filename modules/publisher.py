import asyncio
import logging
import random

import aiohttp
import aiosqlite

from config import DB_PATH, DELAY_MAKE_WEBHOOK, MAKE_PIN_LINK, MAKE_WEBHOOK_URL
from modules import sheets

logger = logging.getLogger(__name__)


async def publish_pin(pin_id: int, file_id: str, category: str, board_id: str) -> bool:
    """Send one pin to Make.com webhook. Returns True on success."""
    if not sheets.get_cached():
        try:
            await sheets.load_sheets()
        except Exception as e:
            logger.error(f"Failed to load Sheets: {e}")
            return False

    cat_data = sheets.get_category_data(category)
    if not cat_data:
        logger.error(f"No Sheets data for category: {category}")
        return False

    title = random.choice(cat_data["titles"]) if cat_data["titles"] else ""
    description = random.choice(cat_data["descriptions"]) if cat_data["descriptions"] else ""
    link = cat_data.get("link") or MAKE_PIN_LINK or ""

    download_url = f"https://lh3.googleusercontent.com/d/{file_id}"
    payload = {
        "file_url": download_url,
        "title": title,
        "description": description,
        "link": link,
        "board_id": board_id,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(MAKE_WEBHOOK_URL, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status == 200:
                    async with aiosqlite.connect(DB_PATH) as db:
                        from datetime import datetime, timezone
                        now = datetime.now(timezone.utc).isoformat()
                        await db.execute(
                            "UPDATE pins_schedule SET status = 'published', published_at = ? WHERE id = ?",
                            (now, pin_id),
                        )
                        await db.commit()
                    logger.info(f"Pin {pin_id} published OK")
                    return True
                else:
                    body = await resp.text()
                    logger.error(f"Make webhook returned {resp.status}: {body}")
                    return False
    except Exception as e:
        logger.error(f"Webhook error for pin {pin_id}: {e}")
        return False
