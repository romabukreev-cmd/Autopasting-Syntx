"""
Google Sheets reader via public CSV export.
The sheet must be shared: "Anyone with the link can view".
No credentials needed — rclone is used for Drive, not Sheets API.
"""
import csv
import io
import logging
import urllib.parse

import aiohttp

from config import GSHEETS_ID, GSHEETS_CATEGORIES_SHEET, GSHEETS_PHRASES_SHEET

logger = logging.getLogger(__name__)

_cache: dict = {}
_phrases: list[str] = []


def _csv_url(sheet_name: str) -> str:
    encoded = urllib.parse.quote(sheet_name)
    return f"https://docs.google.com/spreadsheets/d/{GSHEETS_ID}/export?format=csv&sheet={encoded}"


async def _fetch_csv(sheet_name: str) -> str:
    url = _csv_url(sheet_name)
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                raise RuntimeError(
                    f"Sheets CSV fetch failed (sheet={sheet_name}): {resp.status}. "
                    f"Убедись что таблица открыта для просмотра."
                )
            return await resp.text()


def _parse_categories(text: str) -> dict:
    """
    Expected columns (row 1 = header, skipped):
    category | board_id | title_1 | title_2 | title_3 | desc_1 | desc_2 | desc_3 | link
    """
    data = {}
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    for row in rows[1:]:  # skip header
        if len(row) < 1 or not row[0].strip():
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


def _parse_phrases(text: str) -> list[str]:
    """One phrase per row, no header."""
    phrases = []
    reader = csv.reader(io.StringIO(text))
    for row in reader:
        if row and row[0].strip():
            phrases.append(row[0].strip())
    return phrases


async def load_sheets() -> dict:
    global _cache
    text = await _fetch_csv(GSHEETS_CATEGORIES_SHEET)
    _cache = _parse_categories(text)
    logger.info(f"Sheets loaded: {len(_cache)} categories")
    return _cache


async def load_phrases() -> list[str]:
    global _phrases
    text = await _fetch_csv(GSHEETS_PHRASES_SHEET)
    _phrases = _parse_phrases(text)
    logger.info(f"3D phrases loaded: {len(_phrases)}")
    return _phrases


def get_cached() -> dict:
    return _cache


def get_phrases() -> list[str]:
    return _phrases


def get_category_data(category: str) -> dict | None:
    normalized = category.replace("／", "/")

    # Direct match
    result = _cache.get(normalized) or _cache.get(f"ПРОМПТЫ / {normalized}")
    if result:
        return result

    # Fallback: try parent path for sub-categories like "3D Текст/С промптом" → "3D Текст"
    parent = normalized.split("/")[0].strip()
    return _cache.get(parent) or _cache.get(f"ПРОМПТЫ / {parent}")
