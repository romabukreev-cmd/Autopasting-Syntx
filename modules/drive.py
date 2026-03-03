import asyncio
import hashlib
import io
import logging
from functools import partial

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload

from config import GDRIVE_CREDENTIALS_FILE

logger = logging.getLogger(__name__)
SCOPES = ["https://www.googleapis.com/auth/drive"]


def _get_service():
    creds = service_account.Credentials.from_service_account_file(
        GDRIVE_CREDENTIALS_FILE, scopes=SCOPES
    )
    return build("drive", "v3", credentials=creds)


async def _run_sync(func, *args, **kwargs):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, partial(func, *args, **kwargs))


def _list_files_sync(service, folder_id: str) -> list[dict]:
    results = []
    page_token = None
    while True:
        resp = service.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            fields="nextPageToken, files(id, name, mimeType)",
            pageToken=page_token,
        ).execute()
        results.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return results


def _get_folder_id_sync(service, name: str, parent_id: str = None) -> str | None:
    q = f"name = '{name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    if parent_id:
        q += f" and '{parent_id}' in parents"
    resp = service.files().list(q=q, fields="files(id, name)").execute()
    files = resp.get("files", [])
    return files[0]["id"] if files else None


def _create_folder_sync(service, name: str, parent_id: str = None) -> str:
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        meta["parents"] = [parent_id]
    f = service.files().create(body=meta, fields="id").execute()
    return f["id"]


def _download_file_sync(service, file_id: str) -> bytes:
    request = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()


def _upload_file_sync(service, name: str, mime: str, data: bytes, parent_id: str) -> str:
    buf = io.BytesIO(data)
    media = MediaFileUpload.__new__(MediaFileUpload)
    # Use resumable upload via BytesIO
    from googleapiclient.http import MediaIoBaseUpload
    media = MediaIoBaseUpload(buf, mimetype=mime)
    meta = {"name": name, "parents": [parent_id]}
    f = service.files().create(body=meta, media_body=media, fields="id").execute()
    return f["id"]


def _delete_file_sync(service, file_id: str):
    service.files().delete(fileId=file_id).execute()


def _compute_md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


# --- Async public API ---

async def list_files(folder_id: str) -> list[dict]:
    service = _get_service()
    return await _run_sync(_list_files_sync, service, folder_id)


async def get_folder_id(name: str, parent_id: str = None) -> str | None:
    service = _get_service()
    return await _run_sync(_get_folder_id_sync, service, name, parent_id)


async def get_or_create_folder(name: str, parent_id: str = None) -> str:
    service = _get_service()
    folder_id = await _run_sync(_get_folder_id_sync, service, name, parent_id)
    if folder_id:
        return folder_id
    return await _run_sync(_create_folder_sync, service, name, parent_id)


async def download_file(file_id: str) -> bytes:
    service = _get_service()
    return await _run_sync(_download_file_sync, service, file_id)


async def upload_file(name: str, mime: str, data: bytes, parent_id: str) -> str:
    service = _get_service()
    return await _run_sync(_upload_file_sync, service, name, mime, data, parent_id)


async def delete_file(file_id: str):
    service = _get_service()
    await _run_sync(_delete_file_sync, service, file_id)


async def compute_md5(data: bytes) -> str:
    return _compute_md5(data)
