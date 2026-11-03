"""Safe upload storage.

Security posture
----------------
* filenames are sanitised and never used to build a path directly;
* uploads are stored under a per-document directory keyed by an opaque id;
* the resolved path is asserted to stay inside the configured upload root
  (defence against ``..`` traversal and absolute-path injection);
* stored files are written without the executable bit and are never executed;
* the raw filesystem path is kept server-side only -- the API exposes ids.
"""
from __future__ import annotations

import re
import shutil
import time
import unicodedata
from pathlib import Path
from typing import BinaryIO

from app.config.settings import settings
from app.core.logging import get_logger
from app.core.errors import FileTooLarge, UnsupportedFileType, ValidationFailure

log = get_logger("ingestion.storage")

_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
_MULTI_DOT = re.compile(r"\.{2,}")
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_CHUNK = 1024 * 1024


def sanitize_filename(name: str, *, fallback: str = "document.pdf") -> str:
    """Reduce an arbitrary client-supplied filename to a safe basename."""
    if not name:
        return fallback
    # strip any directory component supplied by the client (both separators)
    name = name.replace("\\", "/").split("/")[-1]
    name = unicodedata.normalize("NFKD", name)
    name = name.encode("ascii", "ignore").decode("ascii")
    name = _SAFE_CHARS.sub("_", name).strip("._ ")
    name = _MULTI_DOT.sub(".", name)
    if not name:
        return fallback
    stem, dot, ext = name.rpartition(".")
    if dot and stem.upper() in _WINDOWS_RESERVED:
        stem = f"file_{stem}"
        name = f"{stem}.{ext}"
    elif not dot and name.upper() in _WINDOWS_RESERVED:
        name = f"file_{name}"
    return name[:200] or fallback


def validate_extension(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext not in settings.allowed_extensions:
        raise UnsupportedFileType(
            f"File type '{ext or 'unknown'}' is not supported. "
            f"Allowed: {', '.join(settings.allowed_extensions)}",
            detail={"filename": filename, "extension": ext},
        )
    return ext


def document_dir(document_id: str) -> Path:
    """Per-document upload directory, guaranteed to live under the upload root."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{4,64}", document_id):
        raise ValidationFailure("Invalid document id", detail={"id": document_id})
    root = settings.upload_dir.resolve()
    target = (root / document_id).resolve()
    if not str(target).startswith(str(root)):
        raise ValidationFailure("Path traversal attempt blocked",
                                detail={"id": document_id})
    return target


def store_stream(document_id: str, filename: str, stream: BinaryIO) -> tuple[Path, int]:
    """Stream an upload to disk, enforcing the size cap without buffering it all.

    Returns ``(path, size_bytes)``.  The partial file is removed if the cap is
    exceeded so a rejected upload leaves nothing behind.
    """
    safe = sanitize_filename(filename)
    validate_extension(safe)
    d = document_dir(document_id)
    d.mkdir(parents=True, exist_ok=True)
    dest = d / safe
    limit = settings.max_upload_mb * 1024 * 1024

    size = 0
    try:
        with open(dest, "wb") as out:
            while True:
                chunk = stream.read(_CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    out.close()
                    dest.unlink(missing_ok=True)
                    raise FileTooLarge(
                        f"File exceeds the {settings.max_upload_mb} MB limit",
                        detail={"limit_mb": settings.max_upload_mb})
                out.write(chunk)
    except FileTooLarge:
        raise
    except OSError as exc:
        dest.unlink(missing_ok=True)
        raise ValidationFailure(f"Could not store upload: {exc}") from exc

    if size == 0:
        dest.unlink(missing_ok=True)
        raise ValidationFailure("Uploaded file is empty")
    return dest, size


def store_local_file(document_id: str, source: str | Path) -> tuple[Path, int]:
    """Ingest a file already on disk (used by CLI ingest / benchmarks)."""
    src = Path(source).resolve()
    if not src.is_file():
        raise ValidationFailure(f"No such file: {src}")
    with open(src, "rb") as fh:
        return store_stream(document_id, src.name, fh)


def delete_document_files(document_id: str, *, attempts: int = 5) -> bool:
    """Remove a document's directory: the PDF, the renders, the thumbnails.

    ``ignore_errors`` alone is not enough on Windows, where a file handle just
    released by a reader can keep the directory locked for a moment.  The
    contents would go and the empty directory would stay, silently, leaving an
    orphan the caller had been told did not exist.  So this retries briefly and
    then reports what actually happened rather than assuming.

    Returns True when the directory is gone.
    """
    d = document_dir(document_id)
    for attempt in range(attempts):
        if not d.exists():
            return True
        shutil.rmtree(d, ignore_errors=True)
        if not d.exists():
            return True
        # a handle that is about to close; give it a moment rather than
        # declaring victory over a directory that is still there
        time.sleep(0.1 * (attempt + 1))
    remaining = list(d.rglob("*")) if d.exists() else []
    log.warning("storage.delete_incomplete", document_id=document_id,
                path=str(d), files_left=len(remaining))
    return not d.exists()
