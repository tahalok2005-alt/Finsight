"""Document ingestion, status, pages and imagery."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, Query, UploadFile
from fastapi.responses import FileResponse, Response
from sqlalchemy import select

from app.api.deps import DbSession, PaginationDep, get_document_or_404, page_envelope
from app.config.settings import settings
from app.core.errors import (
    ConflictError, ResourceNotFound, ValidationFailure,
)
from app.core.geometry import BBox
from app.core.ids import new_uuid, sha256_file
from app.core.logging import get_logger
from app.database import repository as repo
from app.database.models import Page, ProcessingRun, StageRun
from app.ingestion import storage
from app.ingestion.validator import probe_pdf
from app.models.enums import DocumentStatus, PIPELINE_ORDER
from app.provenance import service as prov_service
from app.rendering.renderer import PageRenderer
from app.workers.queue import queue

router = APIRouter(prefix="/documents", tags=["documents"])
log = get_logger("api.documents")


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
        "created_at": doc.created_at, "updated_at": doc.updated_at,
        "processing_started_at": doc.processing_started_at,
        "processing_finished_at": doc.processing_finished_at,
        "processing_ms": doc.processing_ms, "error": doc.error,
        "pages_processed": doc.pages_processed, "pages_failed": doc.pages_failed,
    }
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
    items = [_document_dto(d, repo.document_counts(session, d.id)) for d in rows]
    return page_envelope(items, total, pagination)


@router.get("/{document_id}")
def get_document(document_id: str, session: DbSession) -> dict:
    doc = get_document_or_404(document_id, session)
    counts = repo.document_counts(session, document_id)
    sections = (doc.pdf_metadata or {}).get("sections", [])
    return {**_document_dto(doc, counts), "sections": sections}


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
             "tables", "charts", "extraction", "validating_facts"])}
        rows.sort(key=lambda s: order.get(s.stage, 99))
        for s in rows:
            stages.append({
                "stage": s.stage, "status": s.status, "engine": s.engine,
                "items_total": s.items_total, "items_done": s.items_done,
                "items_failed": s.items_failed, "duration_ms": s.duration_ms,
                "error": s.error, "detail": s.detail,
                "finished_at": s.finished_at})
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
        "error": doc.error, "updated_at": doc.updated_at,
        "run_id": run.id if run else None,
        "engine_versions": run.engine_versions if run else {},
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


@router.delete("/{document_id}", status_code=204)
def delete_document(document_id: str, session: DbSession) -> Response:
    get_document_or_404(document_id, session)
    repo.delete_document(session, document_id)
    session.commit()
    storage.delete_document_files(document_id)
    return Response(status_code=204)


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
