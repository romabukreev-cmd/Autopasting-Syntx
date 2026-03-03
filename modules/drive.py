"""
Google Drive module via rclone.
rclone is already configured on the server with remote 'gdrive:'.
No credentials.json needed.
"""
import asyncio
import hashlib
import json
import logging
import os
import tempfile

logger = logging.getLogger(__name__)

REMOTE = "gdrive:"


async def _rclone(*args: str) -> str:
    """Run rclone command, return stdout. Raises on non-zero exit."""
    cmd = ["rclone"] + list(args)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"rclone {' '.join(args)} failed: {stderr.decode()}")
    return stdout.decode()


def _remote(path: str) -> str:
    """Build full remote path."""
    return f"{REMOTE}{path}"


async def list_folder(path: str) -> list[dict]:
    """
    List files and folders at path.
    Returns list of dicts: {name, id, mimeType, isDir, md5}
    """
    out = await _rclone("lsjson", _remote(path), "--drive-use-trash=false")
    items = json.loads(out)
    result = []
    for item in items:
        result.append({
            "name": item.get("Name", ""),
            "id": item.get("ID", ""),
            "mime_type": item.get("MimeType", ""),
            "is_dir": item.get("IsDir", False),
            "md5": item.get("Hashes", {}).get("md5", ""),
        })
    return result


async def list_files(path: str) -> list[dict]:
    """List only files (not dirs) at path."""
    items = await list_folder(path)
    return [i for i in items if not i["is_dir"]]


async def list_dirs(path: str) -> list[dict]:
    """List only subdirectories at path."""
    items = await list_folder(path)
    return [i for i in items if i["is_dir"]]


async def download_file(remote_path: str) -> bytes:
    """Download file from Drive, return bytes."""
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp_path = tmp.name
    try:
        await _rclone("copyto", _remote(remote_path), tmp_path)
        with open(tmp_path, "rb") as f:
            return f.read()
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


async def upload_file(local_data: bytes, remote_path: str) -> str:
    """
    Upload bytes to Drive at remote_path.
    Returns Google Drive file ID.
    """
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp.write(local_data)
        tmp_path = tmp.name
    try:
        await _rclone("copyto", tmp_path, _remote(remote_path))
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

    # Get file ID after upload
    parent = "/".join(remote_path.split("/")[:-1])
    filename = remote_path.split("/")[-1]
    items = await list_files(parent)
    for item in items:
        if item["name"] == filename:
            return item["id"]
    return ""


async def delete_file(remote_path: str):
    """Delete file from Drive."""
    await _rclone("deletefile", _remote(remote_path))


async def mkdir(remote_path: str):
    """Create folder (and parents) on Drive."""
    await _rclone("mkdir", _remote(remote_path))


async def purge_folder(path: str):
    """Delete entire folder and all its contents from Drive. Silently ignores if not found."""
    try:
        await _rclone("purge", _remote(path))
    except RuntimeError as e:
        if "directory not found" in str(e).lower() or "not found" in str(e).lower():
            pass
        else:
            raise


async def get_file_id(remote_path: str) -> str:
    """Get Google Drive file ID by path."""
    parent = "/".join(remote_path.split("/")[:-1])
    filename = remote_path.split("/")[-1]
    items = await list_files(parent)
    for item in items:
        if item["name"] == filename:
            return item["id"]
    return ""


async def compute_md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()
