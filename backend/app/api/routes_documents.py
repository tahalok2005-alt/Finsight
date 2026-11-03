"""Document ingestion, status, pages and imagery."""
from __future__ import annotations

import time
from datetime import timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, Query, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.deps import DbSession, PaginationDep, get_document_or_404, page_envelope
from app.config.settings import settings
from app.core.errors import (
    ConflictError, ResourceNotFound, ValidationFailure,
)
from app.core.geometry import BBox
from app.core.ids import new_uuid, sha256_file
from app.core.logging import get_logger
from app.database import repository as repo
from app.database.models import (
    FinancialFactRow, Page, ProcessingRun, ProvenanceRow, RegionRow, StageRun,
)
from app.database.models_intelligence import FactRelationRow
from app.ingestion import storage
from app.ingestion.validator import probe_pdf
from app.models.enums import DocumentStatus, PIPELINE_ORDER
from app.provenance import service as prov_service
from app.rendering.renderer import PageRenderer
from app.workers.queue import queue

router = APIRouter(prefix="/documents", tags=["documents"])
log = get_logger("api.documents")


def _iso(dt) -> str | None:
    """A timestamp the browser cannot misread.

    Every time is stored in UTC, but SQLite drops the tzinfo, so the value read
    back is naive.  Serialised naive, ``new Date()`` in the browser interprets
    it as *local* time -- which on a UTC+5:30 machine reported a run that
    started two minutes ago as having run for five and a half hours.  Marking
    the zone explicitly is the fix, and it belongs here rather than in the
    client, which has no way to know.
    """
    if dt is None:
        return None
    if getattr(dt, "tzinfo", None) is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _document_dto(doc, counts: dict | None = None) -> dict[str, Any]:
    d = {
        "id": doc.id, "filename": doc.filename,
        "original_filename": doc.original_filename,
        "content_hash": doc.content_hash, "size_bytes": doc.size_bytes,
        "page_count": doc.page_count, "status": doc.status,
        "title": doc.title, "company_name": doc.company_name,
        "report_period": doc.report_period, "report_type": doc.report_type,
        "document_currency": doc.document_currency,
        "document_unit": doc.document_unit, "is_scanned": doc.is_scanned,
        "created_at": _iso(doc.created_at), "updated_at": _iso(doc.updated_at),
        "processing_started_at": _iso(doc.processing_started_at),
        "processing_finished_at": _iso(doc.processing_finished_at),
        "processing_ms": doc.processing_ms, "error": doc.error,
        "pages_processed": doc.pages_processed, "pages_failed": doc.pages_failed,
        # where a stopped run got to, so a list row explains itself
        "progress_note": doc.progress_note,
    }
    # The lifecycle rules live on the server; the list needs them per row so it
    # can render the right actions without re-deriving the state machine.
    d.update(_control_state(doc))
    if counts:
        d.update(counts)
    return d


@router.post("", status_code=201)
async def upload_document(session: DbSession,
                          file: UploadFile = File(...),
                          allow_duplicate: bool = Form(False)) -> dict:
    """Accept a PDF, validate it, and queue processing.

    Returns immediately: extraction is a background job, and the returned
    status is the real one (``QUEUED``), never a premature success.
    """
    if not file.filename:
        raise ValidationFailure("A filename is required")

    document_id = new_uuid()[:24]
    stored_path, size = storage.store_stream(document_id, file.filename, file.file)

    try:
        content_hash = sha256_file(stored_path)
        existing = repo.find_by_hash(session, content_hash)
        if existing is not None and not allow_duplicate:
            storage.delete_document_files(document_id)
            raise ConflictError(
                "This document has already been ingested.",
                detail={"document_id": existing.id,
                        "status": existing.status,
                        "original_filename": existing.original_filename,
                        "hint": "resubmit with allow_duplicate=true to force"})

        # Fail fast and honestly on an unusable PDF, before any heavy work.
        probe = probe_pdf(stored_path)

        doc = repo.create_document(
            session, document_id=document_id,
            filename=stored_path.name, original_filename=file.filename,
            stored_path=str(stored_path), content_hash=content_hash,
            size_bytes=size,
            settings_snapshot={"render_dpi": settings.render_dpi,
                               "ocr_engine": settings.ocr_engine})
        doc.page_count = probe.page_count
        doc.pdf_version = probe.pdf_version
        doc.is_scanned = probe.is_scanned
        doc.text_layer_chars = probe.text_layer_chars
        doc.pdf_metadata = probe.metadata
        session.flush()
        session.commit()
    except Exception:
        storage.delete_document_files(document_id)
        raise

    job_id = queue.submit(document_id)
    log.info("api.document_uploaded", document_id=document_id,
             pages=probe.page_count, size_bytes=size, job_id=job_id)
    session.expire_all()
    doc = repo.get_document(session, document_id)
    return {**_document_dto(doc), "job_id": job_id,
            "warnings": probe.warnings[:10]}


@router.get("")
def list_documents(session: DbSession, pagination: PaginationDep,
                   status: str | None = Query(None),
                   search: str | None = Query(None)) -> dict:
    rows, total = repo.list_documents(
        session, limit=pagination.limit, offset=pagination.offset,
        status=status, search=search)
    counts = repo.document_counts_bulk(session, [d.id for d in rows])
    items = [_document_dto(d, counts.get(d.id)) for d in rows]
    return page_envelope(items, total, pagination)


@router.get("/{document_id}")
def get_document(document_id: str, session: DbSession) -> dict:
    doc = get_document_or_404(document_id, session)
    counts = repo.document_counts(session, document_id)
    sections = (doc.pdf_metadata or {}).get("sections", [])
    return {**_document_dto(doc, counts), "sections": sections}


@router.get("/{document_id}/diagnostics")
def get_diagnostics(document_id: str, session: DbSession) -> dict:
    """Where a run's time actually went.

    Built for the question "why did this take fourteen minutes", which a
    progress bar cannot answer. Stage totals say which phase dominated; the
    page distribution says whether the cost was spread evenly or concentrated
    in a handful of pages, which are different problems with different fixes.

    Percentiles rather than an average alone: a mean hides the case this is
    meant to catch, where most pages are quick and three are pathological.
    """
    get_document_or_404(document_id, session)
    run = session.scalars(
        select(ProcessingRun).where(ProcessingRun.document_id == document_id)
        .order_by(ProcessingRun.started_at.desc()).limit(1)).first()

    stages = []
    if run is not None:
        for s in session.scalars(select(StageRun).where(StageRun.run_id == run.id)):
            stages.append({"stage": s.stage, "status": s.status,
                           "duration_ms": s.duration_ms,
                           "items_total": s.items_total,
                           "items_done": s.items_done,
                           "items_failed": s.items_failed})
        stages.sort(key=lambda x: -(x["duration_ms"] or 0))

    pages = list(session.scalars(
        select(Page).where(Page.document_id == document_id)
        .order_by(Page.page_number)))
    timed = [p for p in pages if p.duration_ms is not None]
    times = sorted(p.duration_ms for p in timed)

    def pct(fraction: float) -> float | None:
        if not times:
            return None
        idx = min(len(times) - 1, int(round(fraction * (len(times) - 1))))
        return round(times[idx], 1)

    slowest = sorted(timed, key=lambda p: -(p.duration_ms or 0))[:10]

    regions_by_type: dict[str, int] = {}
    for rtype, count in session.execute(
            select(RegionRow.type, func.count())
            .where(RegionRow.document_id == document_id)
            .group_by(RegionRow.type)):
        regions_by_type[str(rtype)] = int(count)
    regions_by_type = dict(sorted(regions_by_type.items(),
                                  key=lambda kv: -kv[1]))

    # Which strategy read each fact.  ``extraction_method`` on the provenance
    # is the record; ``kind`` is the fallback for facts written before the
    # strategy was recorded, and saying so beats silently merging the two.
    facts_by_strategy: dict[str, int] = {}
    for method, kind, count in session.execute(
            select(ProvenanceRow.extraction_method, FinancialFactRow.kind,
                   func.count())
            .join(FinancialFactRow,
                  FinancialFactRow.id == ProvenanceRow.fact_id)
            .where(FinancialFactRow.document_id == document_id)
            .group_by(ProvenanceRow.extraction_method,
                      FinancialFactRow.kind)):
        key = str(method or kind or "unrecorded")
        facts_by_strategy[key] = facts_by_strategy.get(key, 0) + int(count)
    facts_by_strategy = dict(sorted(facts_by_strategy.items(),
                                    key=lambda kv: -kv[1]))

    # ---- what was understood, as counts a reader can act on ---------------
    def _count(*conds) -> int:
        return int(session.execute(
            select(func.count()).select_from(FinancialFactRow)
            .where(FinancialFactRow.document_id == document_id, *conds)
        ).scalar() or 0)

    total_facts = _count()
    understanding = {
        "pages_processed": len(pages),
        "figures_extracted": total_facts,
        "tables_understood": int(session.execute(
            select(func.count(func.distinct(FinancialFactRow.table_id)))
            .where(FinancialFactRow.document_id == document_id,
                   FinancialFactRow.table_id.isnot(None))).scalar() or 0),
        "figures_requiring_review": _count(
            FinancialFactRow.requires_review.is_(True)),
        "figures_without_a_period": _count(FinancialFactRow.period.is_(None)),
        "figures_without_an_entity": _count(FinancialFactRow.entity.is_(None)),
        "figures_corroborated": _count(
            FinancialFactRow.duplicate_resolution == "identical_repeat"),
        "contradictions": int(session.execute(
            select(func.count()).select_from(FactRelationRow)
            .where(FactRelationRow.document_id == document_id,
                   FactRelationRow.kind == "conflicts_with")).scalar() or 0),
        "processing_ms": run.duration_ms if run else None,
    }

    chart_stage = next((st for st in stages if st["stage"] == "charts"), None)
    chart_detail: dict = {}
    if run is not None:
        chart_detail = next(
            (r.detail or {} for r in session.scalars(select(StageRun).where(
                StageRun.run_id == run.id, StageRun.stage == "charts"))), {})
    furniture_count = int(chart_detail.get("page_furniture_suppressed", 0) or 0)
    furniture_templates = chart_detail.get("page_furniture_templates", [])
    analysed_graphics = (chart_stage or {}).get("items_total", 0)
    chart_triage = chart_detail.get("triage") or {}
    understanding["charts_detected"] = analysed_graphics
    understanding["charts_analytically_readable"] = int(
        chart_triage.get("extraction_possible", 0) or 0)

    return {
        "document_id": document_id,
        "run_id": run.id if run else None,
        "total_ms": run.duration_ms if run else None,
        "stages": stages,
        "pages": {
            "counted": len(pages),
            "timed": len(timed),
            "mean_ms": (round(sum(times) / len(times), 1) if times else None),
            "median_ms": pct(0.5),
            "p95_ms": pct(0.95),
            "max_ms": (round(times[-1], 1) if times else None),
            "slow_page_threshold_ms": settings.slow_page_ms,
            "slow_pages": [p.page_number for p in timed
                           if (p.duration_ms or 0) > settings.slow_page_ms],
        },
        # What each page was offered to, what it produced, and why the rest was
        # refused.  Recorded by the extraction stage; surfaced here so "this
        # page produced nothing" is answerable without reading the logs.
        "extraction_by_page": next(
            (s.detail.get("per_page", {}) for s in
             (session.scalars(select(StageRun).where(
                 StageRun.run_id == run.id,
                 StageRun.stage == "extraction")).all() if run else [])
             if s.detail), {}),
        "slowest_pages": [{
            "page_number": p.page_number, "duration_ms": p.duration_ms,
            "ocr_duration_ms": p.ocr_duration_ms,
            "ocr_elements": p.ocr_element_count,
            "regions": p.region_count, "tables": p.table_count,
            "charts": p.chart_count, "status": p.status,
        } for p in slowest],
        # What FinSight actually understood from this document.
        #
        # Every line answers a question an analyst would ask before trusting
        # it, and nothing here is a vanity statistic: a confidence histogram
        # and a most-extracted-metrics chart were both removed in an earlier
        # phase because neither changed any decision.
        "understanding": understanding,
        # What the page analysis saw, and what each strategy made of it.
        # Region counts alone say a document has 197 charts; pairing them with
        # the facts each strategy produced says whether that mattered.
        "regions_by_type": regions_by_type,
        "facts_by_strategy": facts_by_strategy,
        "graphics": {
            "detected": regions_by_type.get("chart", 0)
                        + regions_by_type.get("figure", 0),
            "suppressed_as_page_furniture": furniture_count,
            "analysed": analysed_graphics,
            "page_furniture_templates": furniture_templates,
            "triage": chart_triage,
            "note": ("A block that recurs at one position across several pages "
                     "and holds no data of its own is part of the page "
                     "template, not content. Those are counted and named here "
                     "rather than measured as charts."),
        },
        "note": ("Rendering, OCR and layout run interleaved in one parallel "
                 "loop, so their stage durations overlap each other and sum to "
                 "more than the wall clock. Everything after the page loop is "
                 "sequential and its durations are plain wall time."),
    }


@router.get("/{document_id}/status")
def get_status(document_id: str, session: DbSession) -> dict:
    """Live processing status with per-stage progress.

    Progress is computed from stages that actually completed -- it is never a
    timer or an estimate.
    """
    doc = get_document_or_404(document_id, session)
    run = session.scalars(
        select(ProcessingRun).where(ProcessingRun.document_id == document_id)
        .order_by(ProcessingRun.started_at.desc()).limit(1)).first()

    stages: list[dict] = []
    current = None
    if run is not None:
        rows = list(session.scalars(
            select(StageRun).where(StageRun.run_id == run.id)))
        order = {name: i for i, name in enumerate(
            ["validating", "rendering", "ocr", "layout", "document_assembly",
             "tables", "charts", "extraction", "validating_facts",
             "analysis"])}
        rows.sort(key=lambda s: order.get(s.stage, 99))
        for s in rows:
            stages.append({
                "stage": s.stage, "status": s.status, "engine": s.engine,
                "items_total": s.items_total, "items_done": s.items_done,
                "items_failed": s.items_failed, "duration_ms": s.duration_ms,
                "error": s.error, "detail": s.detail,
                "finished_at": _iso(s.finished_at)})
            if s.status == "RUNNING":
                current = s.stage

    expected = len(PIPELINE_ORDER) + 2  # + assembly + charts
    done = sum(1 for s in stages if s["status"] in ("SUCCEEDED", "PARTIAL"))
    if doc.status in (DocumentStatus.COMPLETED.value,
                      DocumentStatus.PARTIALLY_COMPLETED.value,
                      DocumentStatus.REQUIRES_REVIEW.value):
        progress = 1.0
    elif doc.status == DocumentStatus.FAILED.value:
        progress = round(min(1.0, done / expected), 3)
    else:
        running = next((s for s in stages if s["status"] == "RUNNING"), None)
        partial = 0.0
        if running and running["items_total"]:
            partial = running["items_done"] / running["items_total"]
        progress = round(min(0.99, (done + partial) / expected), 3)

    pages_total = doc.page_count or 0
    page_stage = next((s for s in stages if s["stage"] == "ocr"), None)
    return {
        "document_id": document_id, "status": doc.status, "progress": progress,
        "current_stage": current, "stages": stages,
        "pages_total": pages_total,
        "pages_done": (page_stage or {}).get("items_done", doc.pages_processed),
        "pages_failed": (page_stage or {}).get("items_failed", doc.pages_failed),
        "error": doc.error, "updated_at": _iso(doc.updated_at),
        "run_id": run.id if run else None,
        "engine_versions": run.engine_versions if run else {},
        # which processing mode produced this result, and whether any
        # native PDF text reached extraction -- the audit trail for the
        # visual-first claim
        "processing_mode": (run.settings_snapshot or {}).get(
            "processing_mode") if run else None,
        "native_text_audit": (run.settings_snapshot or {}).get(
            "native_text_audit", {}) if run else {},
    }


@router.post("/{document_id}/reprocess", status_code=202)
def reprocess(document_id: str, session: DbSession,
              force_ocr: bool = Query(False,
                                      description="Ignore the OCR cache")) -> dict:
    doc = get_document_or_404(document_id, session)
    if not Path(doc.stored_path).is_file():
        raise ResourceNotFound("The stored PDF for this document is missing; "
                               "it cannot be reprocessed.")
    job_id = queue.submit(document_id, job_type="reprocess", force_ocr=force_ocr)
    return {"document_id": document_id, "job_id": job_id, "status": "QUEUED",
            "force_ocr": force_ocr}


# ------------------------------------------------------------------- control
def _control_state(doc) -> dict:
    """Which lifecycle actions are legal for this document right now.

    Computed on the server so the UI never has to re-derive the rules and drift
    from them.  A button the backend would refuse is a button the user should
    not be offered.
    """
    status = DocumentStatus(doc.status)
    live = queue.is_controllable(doc.id)
    return {
        "can_pause": status.is_active and status != DocumentStatus.PAUSED and live,
        "can_resume": status == DocumentStatus.PAUSED and live,
        "can_cancel": status.is_active and live,
        "can_requeue": status in (DocumentStatus.CANCELLED, DocumentStatus.FAILED,
                                  DocumentStatus.INTERRUPTED,
                                  DocumentStatus.UPLOADED),
        "can_open": status.has_results,
        "can_delete": True,
    }


@router.get("/{document_id}/control")
def get_control(document_id: str, session: DbSession) -> dict:
    doc = get_document_or_404(document_id, session)
    return {"document_id": document_id, "status": doc.status,
            "progress_note": doc.progress_note, **_control_state(doc)}


@router.post("/{document_id}/pause", status_code=202)
def pause(document_id: str, session: DbSession) -> dict:
    """Ask a running document to stop at its next safe point.

    Returns immediately; the run comes to rest within one page per worker, and
    the status becomes ``PAUSED`` at that moment rather than at this call.
    """
    doc = get_document_or_404(document_id, session)
    status = DocumentStatus(doc.status)
    if status == DocumentStatus.PAUSED:
        return {"document_id": document_id, "status": doc.status,
                "detail": "Already paused."}
    if not status.is_active:
        raise ConflictError(
            f"This document is {doc.status}; only a document that is still "
            "processing can be paused.")
    if not queue.request_pause(document_id):
        raise ConflictError(
            "No live run is attached to this document, so it cannot be paused. "
            "It may have finished, or the server may have restarted.")
    return {"document_id": document_id, "status": "PAUSING",
            "detail": "Pausing at the next page boundary."}


@router.post("/{document_id}/resume", status_code=202)
def resume(document_id: str, session: DbSession) -> dict:
    doc = get_document_or_404(document_id, session)
    if DocumentStatus(doc.status) != DocumentStatus.PAUSED:
        raise ConflictError(
            f"This document is {doc.status}; only a paused document can be "
            "resumed.")
    if not queue.request_resume(document_id):
        raise ConflictError(
            "The paused run is no longer held by this server, most likely "
            "because it restarted. Re-queue the document instead.")
    return {"document_id": document_id, "status": "PROCESSING",
            "detail": "Resuming from where it stopped."}


@router.post("/{document_id}/cancel", status_code=202)
def cancel(document_id: str, session: DbSession) -> dict:
    """Stop a run for good.

    Partial extraction is discarded -- nothing has been persisted at the point
    a run unwinds -- so the document is left with no results rather than with
    half of them.  Rendered pages and OCR output stay in the content-hash
    cache, so re-queueing later is fast.
    """
    doc = get_document_or_404(document_id, session)
    status = DocumentStatus(doc.status)
    if status.is_terminal and status != DocumentStatus.INTERRUPTED:
        return {"document_id": document_id, "status": doc.status,
                "detail": "Nothing to cancel."}
    if not queue.request_cancel(document_id):
        # No live run: the document is stuck in an active-looking state from a
        # previous server lifetime.  Recording the truth is still correct.
        repo.set_status(session, document_id, DocumentStatus.CANCELLED)
        repo.set_progress_note(session, document_id,
                               "Cancelled; no run was in flight.")
        session.commit()
        return {"document_id": document_id, "status": "CANCELLED",
                "detail": "No live run; the document was marked cancelled."}
    return {"document_id": document_id, "status": "CANCELLING",
            "detail": "Stopping at the next page boundary."}


@router.delete("/{document_id}", status_code=204)
def delete_document(document_id: str, session: DbSession) -> Response:
    """Remove a document and everything derived from it.

    A run still in flight is stopped first and given a moment to unwind, so the
    row is never deleted out from under a worker that is still writing to it.
    Deletion then cascades through every child table (pages, OCR elements,
    regions, tables and cells, facts, validations, review items, entities,
    derived metrics, relations, risks, insights, anomalies, runs and stages)
    and finally removes the document's directory -- the uploaded PDF, the
    rendered page images and their thumbnails.
    """
    doc = get_document_or_404(document_id, session)
    _stop_run_before_delete(doc)
    repo.delete_document(session, document_id)
    session.commit()
    # The row is gone either way -- the document is deleted as far as the user
    # is concerned.  A directory that would not go is logged rather than
    # swallowed, so a leak shows up somewhere a person will see it.
    removed = storage.delete_document_files(document_id)
    log.info("documents.deleted", document_id=document_id,
             files_removed=removed)
    return Response(status_code=204)


def _stop_run_before_delete(doc) -> None:
    """Stop a run in flight and wait for it to unwind, or refuse.

    Shared by the single and bulk delete paths on purpose: a second, slightly
    different way to stop a worker is how a bulk action ends up deleting rows
    out from under a thread that is still writing to them.
    """
    document_id = doc.id
    if not (DocumentStatus(doc.status).is_active
            and queue.is_controllable(document_id)):
        return
    log.info("documents.delete_cancels_run", document_id=document_id)
    queue.request_cancel(document_id)
    # Give the run a moment to reach its next checkpoint and unwind.  A page in
    # flight finishes first; that is bounded by one page's OCR.
    deadline = time.monotonic() + settings.delete_cancel_timeout_s
    while queue.is_controllable(document_id) and time.monotonic() < deadline:
        time.sleep(0.1)
    if queue.is_controllable(document_id):
        raise ConflictError(
            "This document is still stopping. Its current page has to "
            "finish before it can be deleted; try again in a moment.")


class BulkDeleteRequest(BaseModel):
    document_ids: list[str] = Field(min_length=1, max_length=500)


@router.post("/bulk-delete")
def bulk_delete(payload: BulkDeleteRequest, session: DbSession) -> dict:
    """Delete several documents, reporting what happened to each.

    Every document is handled through exactly the path a single delete takes,
    including cancelling a run that is still going. One document that cannot be
    stopped in time does not abandon the rest: it is reported as ``busy`` and
    the others still go. Nothing outside ``document_ids`` is touched.
    """
    deleted: list[str] = []
    missing: list[str] = []
    busy: list[str] = []
    failed: list[dict] = []

    for document_id in dict.fromkeys(payload.document_ids):  # de-duplicated
        doc = repo.get_document(session, document_id)
        if doc is None:
            missing.append(document_id)
            continue
        try:
            _stop_run_before_delete(doc)
        except ConflictError:
            busy.append(document_id)
            continue
        try:
            repo.delete_document(session, document_id)
            session.commit()
        except Exception as exc:  # noqa: BLE001 - one failure must not stop the rest
            session.rollback()
            log.exception("documents.bulk_delete_failed",
                          document_id=document_id, error=str(exc))
            failed.append({"document_id": document_id, "error": str(exc)})
            continue
        storage.delete_document_files(document_id)
        deleted.append(document_id)

    log.info("documents.bulk_deleted", requested=len(payload.document_ids),
             deleted=len(deleted), missing=len(missing), busy=len(busy),
             failed=len(failed))
    return {"requested": len(payload.document_ids), "deleted": deleted,
            "missing": missing, "busy": busy, "failed": failed}


# --------------------------------------------------------------------- pages
def _page_dto(p: Page) -> dict:
    return {
        "id": p.id, "document_id": p.document_id, "page_number": p.page_number,
        "pdf_width": p.pdf_width, "pdf_height": p.pdf_height,
        "render_width": p.render_width, "render_height": p.render_height,
        "dpi": p.dpi, "rotation": p.rotation,
        "image_url": prov_service.page_image_url(p.document_id, p.page_number),
        "thumbnail_url": (f"{settings.api_prefix}/documents/{p.document_id}"
                          f"/pages/{p.page_number}/thumbnail"),
        "has_text_layer": p.has_text_layer, "text_layer_chars": p.text_layer_chars,
        "is_scanned": p.is_scanned, "ocr_engine": p.ocr_engine,
        "ocr_element_count": p.ocr_element_count,
        "ocr_mean_confidence": p.ocr_mean_confidence,
        "ocr_duration_ms": p.ocr_duration_ms,
        "image_quality": p.image_quality or {}, "ocr_image": p.ocr_image,
        "region_count": p.region_count, "table_count": p.table_count,
        "chart_count": p.chart_count, "status": p.status, "error": p.error,
    }


@router.get("/{document_id}/pages")
def list_pages(document_id: str, session: DbSession) -> dict:
    get_document_or_404(document_id, session)
    pages = list(session.scalars(
        select(Page).where(Page.document_id == document_id)
        .order_by(Page.page_number)))
    return {"items": [_page_dto(p) for p in pages], "total": len(pages)}


@router.get("/{document_id}/pages/{page_number}")
def get_page(document_id: str, page_number: int, session: DbSession,
             include_ocr: bool = Query(False),
             include_regions: bool = Query(True)) -> dict:
    from app.database.models import OCRElementRow, RegionRow  # noqa: PLC0415

    get_document_or_404(document_id, session)
    page = session.scalars(
        select(Page).where(Page.document_id == document_id,
                           Page.page_number == page_number)).first()
    if page is None:
        raise ResourceNotFound(f"Page {page_number} not found")

    out = _page_dto(page)
    w, h = page.render_width or 1, page.render_height or 1

    if include_regions:
        regions = session.scalars(
            select(RegionRow).where(RegionRow.document_id == document_id,
                                    RegionRow.page_number == page_number)
            .order_by(RegionRow.reading_order))
        out["regions"] = [{
            "id": r.id, "type": r.type, "confidence": r.confidence,
            "reading_order": r.reading_order, "column_index": r.column_index,
            "text": r.text[:2000], "detector": r.detector, "meta": r.meta,
            "bbox": {"x": r.x, "y": r.y, "width": r.width, "height": r.height},
            "bbox_relative": BBox(r.x, r.y, r.width, r.height).to_relative(w, h),
        } for r in regions]

    if include_ocr:
        elements = session.scalars(
            select(OCRElementRow).where(
                OCRElementRow.document_id == document_id,
                OCRElementRow.page_number == page_number)
            .order_by(OCRElementRow.reading_index))
        out["ocr_elements"] = [{
            "id": e.id, "text": e.text, "confidence": e.confidence,
            "corroborated": e.corroborated, "aux_text": e.aux_text,
            "currency_glyph": e.currency_glyph, "region_id": e.region_id,
            "bbox": {"x": e.x, "y": e.y, "width": e.width, "height": e.height},
            "bbox_relative": BBox(e.x, e.y, e.width, e.height).to_relative(w, h),
        } for e in elements]
    return out


def _resolve_page_image(session, document_id: str, page_number: int,
                        thumbnail: bool = False) -> Path:
    doc = get_document_or_404(document_id, session)
    page = session.scalars(
        select(Page).where(Page.document_id == document_id,
                           Page.page_number == page_number)).first()

    renderer = PageRenderer(document_id, doc.stored_path,
                            dpi=(page.dpi if page and page.dpi else None))
    try:
        rendered = renderer.render_page(page_number)
    finally:
        renderer.close()
    path = (rendered.thumbnail_path if thumbnail and rendered.thumbnail_path
            else rendered.image_path)
    if not Path(path).is_file():
        raise ResourceNotFound(f"Image for page {page_number} is unavailable")
    return Path(path)


@router.get("/{document_id}/pages/{page_number}/image")
def page_image(document_id: str, page_number: int, session: DbSession):
    path = _resolve_page_image(session, document_id, page_number)
    return FileResponse(path, media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})


@router.get("/{document_id}/pages/{page_number}/thumbnail")
def page_thumbnail(document_id: str, page_number: int, session: DbSession):
    path = _resolve_page_image(session, document_id, page_number, thumbnail=True)
    return FileResponse(path, media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})


@router.get("/{document_id}/pages/{page_number}/crop")
def page_crop(document_id: str, page_number: int, session: DbSession,
              x: float = Query(...), y: float = Query(...),
              w: float = Query(..., gt=0), h: float = Query(..., gt=0)):
    """Evidence crop for a provenance box -- the exact pixels behind a fact."""
    image_path = _resolve_page_image(session, document_id, page_number)
    crop = prov_service.cached_crop(document_id, page_number, image_path,
                                    BBox(x, y, w, h))
    return FileResponse(crop, media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})
