"""
Google Sheets reader via public CSV export.
The sheet must be shared: "Anyone with the link can view".
No credentials needed — rclone is used for Drive, not Sheets API.
"""
import asyncio
import csv
import io
import logging

import aiohttp

from config import GSHEETS_ID

logger = logging.getLogger(__name__)

_cache: dict = {}


def _csv_url() -> str:
    return f"https://docs.google.com/spreadsheets/d/{GSHEETS_ID}/export?format=csv&gid=0"


def _parse_csv(text: str) -> dict:
    """
    Expected columns (row 1 = header, skipped):
    category | board_id | title_1 | title_2 | title_3 | desc_1 | desc_2 | desc_3 | link
    """
    data = {}
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    for row in rows[1:]:  # skip header
        if len(row) < 2 or not row[0].strip():
            continue
        category = row[0].strip()
        board_id = row[1].strip() if len(row) > 1 else ""
        titles = [row[i].strip() for i in range(2, 5) if i < len(row) and row[i].strip()]
        descriptions = [row[i].strip() for i in range(5, 8) if i < len(row) and row[i].strip()]
        link = row[8].strip() if len(row) > 8 else ""
        data[category] = {
            "board_id": board_id,
            "titles": titles,
            "descriptions": descriptions,
            "link": link,
        }
    return data


async def load_sheets() -> dict:
    global _cache
    url = _csv_url()
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Sheets CSV fetch failed: {resp.status}. Убедись что таблица открыта для просмотра.")
            text = await resp.text()
    _cache = _parse_csv(text)
    logger.info(f"Sheets loaded: {len(_cache)} categories")
    return _cache


def get_cached() -> dict:
    return _cache


def get_category_data(category: str) -> dict | None:
    return _cache.get(category)
