"""Automatic deletion for a public instance.

With ``FINSIGHT_RETENTION_HOURS`` set, a background thread deletes every
document that many hours after it was uploaded -- and, because a document
leaves copies of itself in more places than its own row, everything derived
from it too:

* its database rows, through the same cascade a user's delete uses;
* its upload directory, its page renders and its evidence crops;
* every file in the cache, render and export directories older than the
  retention window. The OCR results and page images are cached by the file's
  checksum, not by document, so they cannot be traced back to one document to
  delete -- but nothing older than the window belongs to a document that is
  still allowed to exist, and every cache rebuilds itself on demand.

That is what makes "your documents are deleted after N hours" true rather than
true of the row only. The cache sweep is skipped while any document is being
processed, so a run is never left reading a file that has just been removed.
"""
from __future__ import annotations

import os
import re
import shutil
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from app.config.settings import settings
from app.core.logging import get_logger
from app.database import repository as repo
from app.database.models import Document
from app.database.session import session_scope
from app.ingestion import storage
from app.models.enums import DocumentStatus

log = get_logger("retention")

_SAFE_ID = re.compile(r"[A-Za-z0-9_-]{4,64}")


def purge_document_artifacts(document_id: str) -> None:
    """Remove a document's own renders and evidence crops.

    The upload directory is removed by ``storage.delete_document_files``; these
    two live elsewhere and used to outlive the document.
    """
    if not _SAFE_ID.fullmatch(document_id):
        return
    for base in (settings.render_dir, settings.cache_dir / "crops"):
        root = Path(base).resolve()
        d = (root / document_id).resolve()
        if d.parent == root and d.is_dir():
            shutil.rmtree(d, ignore_errors=True)


def _aware(dt: datetime) -> datetime:
    # SQLite returns stored timestamps without their zone; they are UTC.
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _purge_old_files(root: Path, cutoff: float) -> int:
    """Delete files under ``root`` last written before ``cutoff``; prune empty
    directories. Returns the number of files removed."""
    removed = 0
    if not root.is_dir():
        return 0
    for dirpath, _dirnames, filenames in os.walk(root, topdown=False):
        for name in filenames:
            p = Path(dirpath) / name
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    removed += 1
            except OSError:
                pass  # vanished or locked; the next sweep tries again
        if Path(dirpath) != root:
            try:
                os.rmdir(dirpath)  # only succeeds when empty
            except OSError:
                pass
    return removed


def sweep_once(now: datetime | None = None) -> dict:
    """Delete everything past retention. Returns what was done."""
    hours = settings.retention_hours
    if not hours or hours <= 0:
        return {"enabled": False}
    from app.workers.queue import queue  # local: the queue imports the pipeline

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)
    deleted: list[str] = []
    deferred: list[str] = []

    with session_scope() as s:
        rows = s.execute(select(Document.id, Document.created_at,
                                Document.status)).all()
    for doc_id, created_at, status in rows:
        if _aware(created_at) >= cutoff:
            continue
        if DocumentStatus(status).is_active and queue.is_controllable(doc_id):
            # A live run owns this document; it is taken on a later sweep.
            deferred.append(doc_id)
            continue
        with session_scope() as s:
            repo.delete_document(s, doc_id)
        storage.delete_document_files(doc_id)
        purge_document_artifacts(doc_id)
        deleted.append(doc_id)

    files = 0
    if queue.active_count == 0:
        stamp = cutoff.timestamp()
        for base in (settings.cache_dir, settings.render_dir, settings.export_dir):
            files += _purge_old_files(Path(base).resolve(), stamp)

    if deleted or files:
        log.info("retention.swept", documents_deleted=len(deleted),
                 cache_files_deleted=files, deferred=len(deferred))
    return {"enabled": True, "documents_deleted": deleted,
            "deferred": deferred, "cache_files_deleted": files}


class RetentionSweeper:
    """Runs ``sweep_once`` on a schedule until stopped."""

    def __init__(self):
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @staticmethod
    def interval_seconds() -> float:
        hours = settings.retention_hours or 0
        # a quarter of the window, but never idle for more than ten minutes
        # and never busier than once a minute
        return max(60.0, min(600.0, hours * 3600 / 4))

    def start(self) -> bool:
        if not settings.retention_hours or self._thread is not None:
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="finsight-retention",
                                        daemon=True)
        self._thread.start()
        log.info("retention.started", hours=settings.retention_hours,
                 interval_s=self.interval_seconds())
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                sweep_once()
            except Exception as exc:  # noqa: BLE001 - logged; the next sweep retries
                log.exception("retention.sweep_failed", error=str(exc))
            self._stop.wait(self.interval_seconds())


sweeper = RetentionSweeper()
