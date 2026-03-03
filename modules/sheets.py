import asyncio
import logging
from functools import partial

from google.oauth2 import service_account
from googleapiclient.discovery import build

from config import GDRIVE_CREDENTIALS_FILE, GSHEETS_ID, GSHEETS_RANGE

logger = logging.getLogger(__name__)
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

# Cache: {category: {board_id, titles, descriptions, link}}
_cache: dict = {}


def _get_service():
    creds = service_account.Credentials.from_service_account_file(
        GDRIVE_CREDENTIALS_FILE, scopes=SCOPES
    )
    return build("sheets", "v4", credentials=creds)


def _read_sheet_sync(service) -> list[list]:
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=GSHEETS_ID, range=GSHEETS_RANGE)
        .execute()
    )
    return result.get("values", [])


def _parse_rows(rows: list[list]) -> dict:
    """
    Expected columns:
    category | board_id | title_1..5 | description_1..5 | link
    """
    data = {}
    for row in rows[1:]:  # skip header
        if len(row) < 3:
            continue
        category = row[0].strip()
        board_id = row[1].strip()
        titles = [row[i].strip() for i in range(2, 7) if i < len(row) and row[i].strip()]
        descriptions = [row[i].strip() for i in range(7, 12) if i < len(row) and row[i].strip()]
        link = row[12].strip() if len(row) > 12 else ""
        data[category] = {
            "board_id": board_id,
            "titles": titles,
            "descriptions": descriptions,
            "link": link,
        }
    return data


async def load_sheets() -> dict:
    global _cache
    loop = asyncio.get_event_loop()
    service = _get_service()
    rows = await loop.run_in_executor(None, partial(_read_sheet_sync, service))
    _cache = _parse_rows(rows)
    logger.info(f"Sheets loaded: {len(_cache)} categories")
    return _cache


def get_cached() -> dict:
    return _cache


def get_category_data(category: str) -> dict | None:
    return _cache.get(category)
