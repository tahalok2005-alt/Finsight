"""Reconciling the database with reality at startup.

Processing state lives partly in memory: the worker threads, and the control
objects the user's pause and cancel requests reach.  A server restart destroys
all of it while the database keeps whatever the last write said.  Any document
recorded as ``PROCESSING`` is therefore a document nothing is working on, and
leaving it that way gives the user a progress view that will never move and
buttons that will never do anything.

So on startup every such document is marked ``INTERRUPTED``: a terminal state
that says plainly what happened and offers the only honest next step, which is
to queue it again.  Rendered pages and OCR output survive in the content-hash
cache, so re-queueing resumes cheaply rather than starting over.
"""
from __future__ import annotations

import shutil

from sqlalchemy import select

from app.config.settings import settings
from app.core.logging import get_logger
from app.database import repository as repo
from app.database.models import Document, ProcessingRun, StageRun
from app.database.session import session_scope
from app.models.enums import DocumentStatus, StageStatus

log = get_logger("services.reconcile")


def reconcile_interrupted_runs() -> int:
    """Mark documents left mid-run by a previous process lifetime."""
    stranded = 0
    with session_scope() as session:
        docs = session.scalars(select(Document)).all()
        for doc in docs:
            try:
                status = DocumentStatus(doc.status)
            except ValueError:            # a status this build no longer knows
                continue
            if not status.is_active:
                continue
            doc.status = str(DocumentStatus.INTERRUPTED)
            doc.progress_note = (
                "Interrupted when the server stopped. Re-queue to continue; "
                "pages already read are cached.")
            stranded += 1

            runs = session.scalars(
                select(ProcessingRun).where(
                    ProcessingRun.document_id == doc.id,
                    ProcessingRun.status == "RUNNING")).all()
            for run in runs:
                run.status = "INTERRUPTED"
                repo.mark_running_stages(session, run.id, StageStatus.CANCELLED)
            log.info("reconcile.interrupted", document_id=doc.id,
                     was=status.value)
    return stranded


def sweep_orphaned_files() -> int:
    """Remove upload directories that no document row owns.

    Deletion removes a document's directory as part of the request, but a
    directory can refuse to go: a synced folder (OneDrive) or an indexer may
    hold a handle for a while after the files inside are gone, and Windows
    answers ``PermissionError`` rather than waiting.  The request cannot block
    on that, so the leftover is picked up here instead -- by the next startup,
    the handle is almost always released.

    Only directories with no matching document row are touched, which is the
    definition of an orphan: nothing in the database can ever refer to them
    again.
    """
    root = settings.upload_dir
    if not root.is_dir():
        return 0
    with session_scope() as session:
        owned = {d.id for d in session.scalars(select(Document))}

    removed = 0
    for path in root.iterdir():
        if not path.is_dir() or path.name in owned:
            continue
        shutil.rmtree(path, ignore_errors=True)
        if path.exists():
            log.info("reconcile.orphan_still_locked", path=str(path))
            continue
        removed += 1
        log.info("reconcile.orphan_removed", path=str(path))
    return removed
