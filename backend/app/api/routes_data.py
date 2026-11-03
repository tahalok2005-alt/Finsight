"""Extracted-data endpoints: facts, tables, charts, commentary, guidance,
validation, provenance, review, export, comparison and search."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Query
from fastapi.responses import Response
from sqlalchemy import func, or_, select

from app.api.deps import DbSession, PaginationDep, get_document_or_404, page_envelope
from app.comparison import service as comparison_service
from app.config.settings import settings
from app.core.errors import ResourceNotFound, ValidationFailure
from app.core.geometry import BBox
from app.core.logging import get_logger
from app.database.models import (
    ChartRow, CommentaryRow, Document, FinancialFactRow, GuidanceRow, Page,
    ProvenanceRow, RegionRow, TableCellRow, TableRow, ValidationResultRow,
)
from app.export import exporters
from app.extraction import taxonomy
from app.models.enums import REVIEW_REASON_TEXT
from app.models.schemas import ReviewRequest
from app.provenance import service as prov_service
from app.review import service as review_service

router = APIRouter(prefix="/documents", tags=["data"])
log = get_logger("api.data")


def _page_dims(session, document_id: str) -> dict[int, tuple[int, int]]:
    rows = session.execute(
        select(Page.page_number, Page.render_width, Page.render_height)
        .where(Page.document_id == document_id)).all()
    return {r[0]: (r[1] or 1, r[2] or 1) for r in rows}


def _fact_dto(f: FinancialFactRow, prov: ProvenanceRow | None,
              dims: dict[int, tuple[int, int]]) -> dict[str, Any]:
    out = {
        "fact_id": f.id, "document_id": f.document_id, "metric": f.metric,
        "canonical_metric": f.canonical_metric,
        "metric_display": f.metric_display or f.metric,
        "entity": f.entity, "kind": f.kind, "nature": f.nature,
        # The reporting basis (standalone / consolidated / segment / unknown)
        # and how reconciliation classified this figure against others in
        # the same slot: a genuine conflict, a different basis, a repeat.
        "entity_type": f.entity_type,
        "duplicate_resolution": f.duplicate_resolution,
        "value": f.value, "corrected_value": f.corrected_value,
        "effective_value": (f.corrected_value if f.corrected_value is not None
                            else f.value),
        "raw_value": f.raw_value, "value_type": f.value_type,
        "currency": f.currency, "unit": f.unit, "base_value": f.base_value,
        # Whether the currency or scale was carried to this figure from the
        # table's declaration or the document rather than printed with it --
        # a rupee glyph OCR cannot read leaves "crore" to imply rupees.
        "currency_inferred": bool(f.currency_inferred),
        "unit_inferred": bool(f.unit_inferred),
        "period": f.period, "period_type": f.period_type,
        "fiscal_year": f.fiscal_year, "quarter": f.quarter,
        "period_raw": f.period_raw,
        # The dates the source actually supports, plus how the period was
        # arrived at and how sure the resolver was.  A null date means the
        # document never established one -- the client must not fill it in.
        "period_start": f.period_start, "period_end": f.period_end,
        "period_confidence": f.period_confidence,
        "period_basis": f.period_basis,
        "comparison_period": f.comparison_period,
        "comparison_value": f.comparison_value,
        "growth": f.growth, "growth_type": f.growth_type,
        "growth_reported": f.growth_reported,
        "confidence": f.confidence, "confidence_band": f.confidence_band,
        "confidence_breakdown": f.confidence_breakdown,
        "validation_status": f.validation_status,
        "requires_review": f.requires_review, "review_state": f.review_state,
        # Not just that a figure needs a look, but what to look at.
        "review_reason": f.review_reason,
        "review_explanation": (REVIEW_REASON_TEXT.get(f.review_reason)
                               if f.review_reason else None),
        "source_page": f.source_page, "original_text": f.original_text,
        "context_text": f.context_text, "meta": f.meta,
        # The basis the figure is stated on, where the source states one:
        # "excluding a one-off gain", "Adjusted" (Phase 22).
        "qualification": (f.meta or {}).get("qualification"),
        "table_id": f.table_id, "cell_id": f.cell_id, "region_id": f.region_id,
    }
    if prov is not None:
        w, h = dims.get(prov.page_number, (1, 1))
        bbox = (BBox(prov.x, prov.y, prov.width, prov.height)
                if prov.x is not None else None)
        out["provenance"] = {
            "document_id": prov.document_id, "page_number": prov.page_number,
            "region_id": prov.region_id, "table_id": prov.table_id,
            "cell_id": prov.cell_id, "chart_id": prov.chart_id,
            "original_text": prov.original_text,
            "ocr_element_ids": prov.ocr_element_ids,
            "extraction_method": prov.extraction_method,
            "bbox": bbox.as_dict() if bbox else None,
            "bbox_relative": bbox.to_relative(w, h) if bbox else None,
            "page_image_url": prov_service.page_image_url(
                prov.document_id, prov.page_number),
            "crop_url": prov_service.crop_url(
                prov.document_id, prov.page_number,
                type("B", (), {"x": prov.x, "y": prov.y,
                               "width": prov.width, "height": prov.height})())
            if bbox else None,
        }
    return out


# ---------------------------------------------------------------- financials
@router.get("/{document_id}/financials")
def get_financials(document_id: str, session: DbSession, pagination: PaginationDep,
                   metric: str | None = Query(None),
                   entity: str | None = Query(None),
                   period: str | None = Query(None),
                   nature: str | None = Query(None),
                   kind: str | None = Query(None),
                   band: str | None = Query(None),
                   validation_status: str | None = Query(None),
                   requires_review: bool | None = Query(None),
                   min_confidence: float | None = Query(None, ge=0, le=1),
                   sort: str = Query("page",
                                     pattern="^(page|confidence|metric|period|value)$"),
                   order: str = Query("asc", pattern="^(asc|desc)$")) -> dict:
    get_document_or_404(document_id, session)
    stmt = select(FinancialFactRow).where(
        FinancialFactRow.document_id == document_id)
    if metric:
        stmt = stmt.where(or_(FinancialFactRow.canonical_metric == metric,
                              func.lower(FinancialFactRow.metric)
                              .like(f"%{metric.lower()}%")))
    if entity:
        stmt = stmt.where(FinancialFactRow.entity == entity)
    if period:
        stmt = stmt.where(FinancialFactRow.period == period)
    if nature:
        stmt = stmt.where(FinancialFactRow.nature == nature)
    if kind:
        stmt = stmt.where(FinancialFactRow.kind == kind)
    if band:
        stmt = stmt.where(FinancialFactRow.confidence_band == band)
    if validation_status:
        stmt = stmt.where(FinancialFactRow.validation_status == validation_status)
    if requires_review is not None:
        stmt = stmt.where(FinancialFactRow.requires_review.is_(requires_review))
    if min_confidence is not None:
        stmt = stmt.where(FinancialFactRow.confidence >= min_confidence)

    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    col = {"page": FinancialFactRow.source_page,
           "confidence": FinancialFactRow.confidence,
           "metric": FinancialFactRow.canonical_metric,
           "period": FinancialFactRow.fiscal_year,
           "value": FinancialFactRow.value}[sort]
    stmt = stmt.order_by(col.desc() if order == "desc" else col.asc())
    rows = list(session.scalars(stmt.limit(pagination.limit)
                                .offset(pagination.offset)))

    prov = {p.fact_id: p for p in session.scalars(
        select(ProvenanceRow).where(ProvenanceRow.document_id == document_id))}
    dims = _page_dims(session, document_id)
    items = [_fact_dto(f, prov.get(f.id), dims) for f in rows]

    facets = {
        "metrics": [r[0] for r in session.execute(
            select(FinancialFactRow.canonical_metric).where(
                FinancialFactRow.document_id == document_id).distinct()).all()],
        "entities": [r[0] for r in session.execute(
            select(FinancialFactRow.entity).where(
                FinancialFactRow.document_id == document_id,
                FinancialFactRow.entity.is_not(None)).distinct()).all()],
        "periods": comparison_service.available_periods(session, document_id),
    }
    return {**page_envelope(items, total, pagination), "facets": facets}


@router.get("/{document_id}/financials/{fact_id}")
def get_fact(document_id: str, fact_id: str, session: DbSession) -> dict:
    get_document_or_404(document_id, session)
    f = session.get(FinancialFactRow, fact_id)
    if f is None or f.document_id != document_id:
        raise ResourceNotFound(f"No fact {fact_id!r} in this document")
    prov = session.scalars(select(ProvenanceRow).where(
        ProvenanceRow.fact_id == fact_id)).first()
    dto = _fact_dto(f, prov, _page_dims(session, document_id))
    dto["validations"] = [{
        "rule": v.rule, "rule_family": v.rule_family, "status": v.status,
        "severity": v.severity, "message": v.message,
        "reported_value": v.reported_value,
        "calculated_value": v.calculated_value, "difference": v.difference,
        "tolerance": v.tolerance,
    } for v in session.scalars(select(ValidationResultRow).where(
        ValidationResultRow.document_id == document_id))
        if fact_id in (v.fact_ids or [])]
    dto["review_history"] = review_service.history(
        session, document_id, target_id=fact_id)
    return dto


# -------------------------------------------------------------------- tables
@router.get("/{document_id}/tables")
def get_tables(document_id: str, session: DbSession,
               page: int | None = Query(None),
               include_cells: bool = Query(True)) -> dict:
    get_document_or_404(document_id, session)
    stmt = select(TableRow).where(TableRow.document_id == document_id)
    if page is not None:
        stmt = stmt.where(TableRow.page_number == page)
    tables = list(session.scalars(stmt.order_by(TableRow.page_number, TableRow.y)))
    dims = _page_dims(session, document_id)

    cells_by_table: dict[str, list[TableCellRow]] = {}
    if include_cells and tables:
        ids = [t.id for t in tables]
        for c in session.scalars(select(TableCellRow).where(
                TableCellRow.table_id.in_(ids))):
            cells_by_table.setdefault(c.table_id, []).append(c)

    items = []
    for t in tables:
        w, h = dims.get(t.page_number, (1, 1))
        box = BBox(t.x, t.y, t.width, t.height)
        cells = sorted(cells_by_table.get(t.id, []), key=lambda z: (z.row, z.col))
        grid = [["" for _ in range(t.n_cols)] for _ in range(t.n_rows)]
        for c in cells:
            if 0 <= c.row < t.n_rows and 0 <= c.col < t.n_cols:
                grid[c.row][c.col] = c.text
        items.append({
            "id": t.id, "page_number": t.page_number, "region_id": t.region_id,
            "title": t.title, "caption": t.caption, "n_rows": t.n_rows,
            "n_cols": t.n_cols, "detector": t.detector,
            "confidence": t.confidence, "has_ruling_lines": t.has_ruling_lines,
            "unit_hint": t.unit_hint, "currency_hint": t.currency_hint,
            "entity": t.entity_hint, "columns": t.columns,
            "warnings": t.warnings,
            "continued_from_table_id": t.continued_from_table_id,
            "bbox": box.as_dict(), "bbox_relative": box.to_relative(w, h),
            "page_image_url": prov_service.page_image_url(document_id, t.page_number),
            "grid": grid,
            "cells": [{
                "id": c.id, "row": c.row, "col": c.col, "text": c.text,
                "role": c.role, "is_numeric": c.is_numeric,
                "numeric_value": c.numeric_value, "confidence": c.confidence,
                "bbox": {"x": c.x, "y": c.y, "width": c.width, "height": c.height},
                "bbox_relative": BBox(c.x, c.y, c.width, c.height).to_relative(w, h),
            } for c in cells] if include_cells else [],
        })
    return {"items": items, "total": len(items)}


# -------------------------------------------------------------------- charts
@router.get("/{document_id}/charts")
def get_charts(document_id: str, session: DbSession) -> dict:
    get_document_or_404(document_id, session)
    dims = _page_dims(session, document_id)
    items = []
    for c in session.scalars(select(ChartRow).where(
            ChartRow.document_id == document_id).order_by(ChartRow.page_number)):
        w, h = dims.get(c.page_number, (1, 1))
        box = BBox(c.x, c.y, c.width, c.height)
        items.append({
            "id": c.id, "page_number": c.page_number, "region_id": c.region_id,
            "chart_type": c.chart_type, "type_confidence": c.type_confidence,
            "title": c.title, "axis_labels": c.axis_labels,
            "legend_labels": c.legend_labels, "categories": c.categories,
            "data_labels": c.data_labels, "unit_hint": c.unit_hint,
            "confidence": c.confidence, "values_extracted": c.values_extracted,
            "requires_review": c.requires_review, "notes": c.notes,
            "bbox": box.as_dict(), "bbox_relative": box.to_relative(w, h),
            "page_image_url": prov_service.page_image_url(document_id, c.page_number),
            "crop_url": prov_service.crop_url(document_id, c.page_number,
                                              type("B", (), {
                                                  "x": c.x, "y": c.y,
                                                  "width": c.width,
                                                  "height": c.height})()),
        })
    return {"items": items, "total": len(items)}


# ---------------------------------------------------------------- commentary
@router.get("/{document_id}/commentary")
def get_commentary(document_id: str, session: DbSession,
                   topic: str | None = Query(None),
                   sentiment: str | None = Query(None)) -> dict:
    get_document_or_404(document_id, session)
    stmt = select(CommentaryRow).where(CommentaryRow.document_id == document_id)
    if topic:
        stmt = stmt.where(CommentaryRow.primary_topic == topic)
    if sentiment:
        stmt = stmt.where(CommentaryRow.sentiment == sentiment)
    rows = list(session.scalars(stmt.order_by(CommentaryRow.page_number)))
    dims = _page_dims(session, document_id)
    items = []
    for c in rows:
        w, h = dims.get(c.page_number, (1, 1))
        box = (BBox(c.x, c.y, c.width, c.height) if c.x is not None else None)
        items.append({
            "id": c.id, "page_number": c.page_number, "region_id": c.region_id,
            "text": c.text, "topics": c.topics, "primary_topic": c.primary_topic,
            "sentiment": c.sentiment, "speaker": c.speaker,
            "is_quote": c.is_quote, "confidence": c.confidence,
            "summary": c.summary, "summary_engine": c.summary_engine,
            "mentioned_metrics": c.mentioned_metrics,
            "bbox": box.as_dict() if box else None,
            "bbox_relative": box.to_relative(w, h) if box else None,
            "page_image_url": prov_service.page_image_url(document_id, c.page_number),
        })
    topics = [{"topic": r[0], "count": r[1]} for r in session.execute(
        select(CommentaryRow.primary_topic, func.count())
        .where(CommentaryRow.document_id == document_id)
        .group_by(CommentaryRow.primary_topic)).all()]
    return {"items": items, "total": len(items), "topics": topics}


# ------------------------------------------------------------------ guidance
@router.get("/{document_id}/guidance")
def get_guidance(document_id: str, session: DbSession) -> dict:
    get_document_or_404(document_id, session)
    dims = _page_dims(session, document_id)
    items = []
    for g in session.scalars(select(GuidanceRow).where(
            GuidanceRow.document_id == document_id).order_by(GuidanceRow.page_number)):
        w, h = dims.get(g.page_number, (1, 1))
        box = (BBox(g.x, g.y, g.width, g.height) if g.x is not None else None)
        items.append({
            "id": g.id, "page_number": g.page_number, "region_id": g.region_id,
            "text": g.text, "nature": g.nature,
            "canonical_metric": g.canonical_metric, "metric": g.metric,
            "target_value": g.target_value, "target_unit": g.target_unit,
            "target_currency": g.target_currency,
            "target_period": g.target_period, "direction": g.direction,
            "trigger_phrase": g.trigger_phrase, "confidence": g.confidence,
            "requires_review": g.requires_review,
            "bbox": box.as_dict() if box else None,
            "bbox_relative": box.to_relative(w, h) if box else None,
            "page_image_url": prov_service.page_image_url(document_id, g.page_number),
        })
    return {"items": items, "total": len(items),
            "note": ("Forward-looking statements are kept separate from "
                     "historical actuals and are never mixed into the fact set.")}


# ---------------------------------------------------------------- validation
@router.get("/{document_id}/validation")
def get_validation(document_id: str, session: DbSession,
                   status: str | None = Query(None),
                   family: str | None = Query(None)) -> dict:
    get_document_or_404(document_id, session)
    stmt = select(ValidationResultRow).where(
        ValidationResultRow.document_id == document_id)
    if status:
        stmt = stmt.where(ValidationResultRow.status == status)
    if family:
        stmt = stmt.where(ValidationResultRow.rule_family == family)
    rows = list(session.scalars(stmt))

    summary: dict[str, int] = {}
    for r in session.execute(
            select(ValidationResultRow.status, func.count())
            .where(ValidationResultRow.document_id == document_id)
            .group_by(ValidationResultRow.status)).all():
        summary[r[0]] = r[1]
    by_family: dict[str, dict[str, int]] = {}
    for r in session.execute(
            select(ValidationResultRow.rule_family, ValidationResultRow.status,
                   func.count())
            .where(ValidationResultRow.document_id == document_id)
            .group_by(ValidationResultRow.rule_family,
                      ValidationResultRow.status)).all():
        by_family.setdefault(r[0], {})[r[1]] = r[2]

    return {
        "items": [{
            "id": v.id, "rule": v.rule, "rule_family": v.rule_family,
            "status": v.status, "severity": v.severity, "message": v.message,
            "reported_value": v.reported_value,
            "calculated_value": v.calculated_value, "difference": v.difference,
            "tolerance": v.tolerance, "fact_ids": v.fact_ids,
            "page_numbers": v.page_numbers, "detail": v.detail,
        } for v in rows],
        "total": len(rows), "summary": summary, "by_family": by_family,
    }


# ---------------------------------------------------------------- provenance
@router.get("/{document_id}/provenance/{fact_id}")
def get_provenance(document_id: str, fact_id: str, session: DbSession) -> dict:
    get_document_or_404(document_id, session)
    prov = session.scalars(select(ProvenanceRow).where(
        ProvenanceRow.document_id == document_id,
        ProvenanceRow.fact_id == fact_id)).first()
    if prov is None:
        raise ResourceNotFound(f"No provenance recorded for fact {fact_id!r}")
    fact = session.get(FinancialFactRow, fact_id)
    page = session.scalars(select(Page).where(
        Page.document_id == document_id,
        Page.page_number == prov.page_number)).first()
    w = page.render_width if page else 1
    h = page.render_height if page else 1
    box = (BBox(prov.x, prov.y, prov.width, prov.height)
           if prov.x is not None else None)

    region = (session.get(RegionRow, prov.region_id) if prov.region_id else None)
    cell = (session.get(TableCellRow, prov.cell_id) if prov.cell_id else None)

    return {
        "fact_id": fact_id, "document_id": document_id,
        "page_number": prov.page_number,
        "metric": fact.metric if fact else None,
        "value": fact.value if fact else None,
        "unit": fact.unit if fact else None,
        "currency": fact.currency if fact else None,
        "period": fact.period if fact else None,
        "confidence": fact.confidence if fact else None,
        "region": ({"id": region.id, "type": region.type,
                    "text": region.text[:1000]} if region else None),
        "cell": ({"id": cell.id, "row": cell.row, "col": cell.col,
                  "text": cell.text, "role": cell.role} if cell else None),
        "table_id": prov.table_id, "chart_id": prov.chart_id,
        "original_text": prov.original_text,
        "ocr_element_ids": prov.ocr_element_ids,
        "extraction_method": prov.extraction_method,
        "bbox": box.as_dict() if box else None,
        "bbox_relative": box.to_relative(w, h) if box else None,
        "page_render": {"width": w, "height": h,
                        "dpi": page.dpi if page else None},
        "page_image_url": prov_service.page_image_url(document_id, prov.page_number),
        "crop_url": (f"{settings.api_prefix}/documents/{document_id}/pages/"
                     f"{prov.page_number}/crop?x={prov.x:.1f}&y={prov.y:.1f}"
                     f"&w={prov.width:.1f}&h={prov.height:.1f}") if box else None,
    }


# -------------------------------------------------------------------- review
@router.get("/{document_id}/reviews")
def get_review_queue(document_id: str, session: DbSession,
                     pagination: PaginationDep,
                     include_resolved: bool = Query(False)) -> dict:
    get_document_or_404(document_id, session)
    return review_service.queue(session, document_id, limit=pagination.limit,
                                offset=pagination.offset,
                                include_resolved=include_resolved)


@router.post("/{document_id}/reviews", status_code=201)
def post_review(document_id: str, session: DbSession,
                payload: ReviewRequest = Body(...)) -> dict:
    get_document_or_404(document_id, session)
    result = review_service.apply_action(
        session, document_id, target_type=payload.target_type,
        target_id=payload.target_id, action=str(payload.action),
        reviewer=payload.reviewer, note=payload.note,
        new_value=payload.new_value)
    session.commit()
    return {"document_id": document_id, "target_id": payload.target_id,
            "action": str(payload.action), "result": result}


@router.get("/{document_id}/reviews/history")
def get_review_history(document_id: str, session: DbSession,
                       target_id: str | None = Query(None)) -> dict:
    get_document_or_404(document_id, session)
    items = review_service.history(session, document_id, target_id=target_id)
    return {"items": items, "total": len(items)}


# -------------------------------------------------------------------- export
@router.get("/{document_id}/export")
def export_document(document_id: str, session: DbSession,
                    format: str = Query("json", pattern="^(json|csv|xlsx)$"),
                    include: str | None = Query(
                        None, description="comma-separated sections for JSON")):
    doc = get_document_or_404(document_id, session)
    exporter = exporters.EXPORTERS[format]
    if format == "json":
        parts = [p.strip() for p in include.split(",")] if include else None
        data = exporter(session, document_id, include=parts)
    else:
        data = exporter(session, document_id)
    stem = (doc.original_filename.rsplit(".", 1)[0] or "finsight")[:60]
    filename = f"{stem}-finsight.{format}"
    return Response(
        content=data, media_type=exporters.MEDIA_TYPES[format],
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ---------------------------------------------------------------- comparison
compare_router = APIRouter(tags=["comparison"])


@compare_router.get("/compare")
def compare_documents(session: DbSession,
                      left: str = Query(...), right: str = Query(...),
                      left_period: str | None = Query(None),
                      right_period: str | None = Query(None),
                      entity: str | None = Query(None),
                      metrics: str | None = Query(None)) -> dict:
    """Compare two processed reports, or two periods within one report."""
    get_document_or_404(left, session)
    get_document_or_404(right, session)
    metric_list = [m.strip() for m in metrics.split(",")] if metrics else None
    return comparison_service.compare(
        session, left_document_id=left, right_document_id=right,
        left_period=left_period, right_period=right_period,
        metrics=metric_list, entity=entity)


# -------------------------------------------------------------------- search
search_router = APIRouter(tags=["search"])


@search_router.get("/search")
def search(session: DbSession, q: str = Query(..., min_length=2),
           document_id: str | None = Query(None),
           limit: int = Query(50, ge=1, le=200)) -> dict:
    """Search across documents, facts, tables, commentary and guidance."""
    like = f"%{q.lower()}%"
    results: dict[str, list[dict]] = {}

    def scope(stmt, model):
        return stmt.where(model.document_id == document_id) if document_id else stmt

    doc_stmt = select(Document).where(or_(
        func.lower(Document.original_filename).like(like),
        func.lower(func.coalesce(Document.title, "")).like(like),
        func.lower(func.coalesce(Document.company_name, "")).like(like),
        func.lower(func.coalesce(Document.report_period, "")).like(like)))
    if document_id:
        doc_stmt = doc_stmt.where(Document.id == document_id)
    results["documents"] = [
        {"id": d.id, "filename": d.original_filename, "title": d.title,
         "company_name": d.company_name, "report_period": d.report_period,
         "status": d.status}
        for d in session.scalars(doc_stmt.limit(limit))]

    fact_stmt = scope(select(FinancialFactRow).where(or_(
        func.lower(FinancialFactRow.metric).like(like),
        func.lower(FinancialFactRow.canonical_metric).like(like),
        func.lower(func.coalesce(FinancialFactRow.period, "")).like(like),
        func.lower(func.coalesce(FinancialFactRow.entity, "")).like(like),
        func.lower(FinancialFactRow.original_text).like(like))),
        FinancialFactRow)
    results["facts"] = [
        {"fact_id": f.id, "document_id": f.document_id, "metric": f.metric,
         "canonical_metric": f.canonical_metric, "value": f.value,
         "unit": f.unit, "currency": f.currency, "period": f.period,
         "entity": f.entity, "confidence": f.confidence,
         "source_page": f.source_page}
        for f in session.scalars(
            fact_stmt.order_by(FinancialFactRow.confidence.desc()).limit(limit))]

    cell_stmt = scope(select(TableCellRow).where(
        func.lower(TableCellRow.text).like(like)), TableCellRow)
    results["table_cells"] = [
        {"id": c.id, "document_id": c.document_id, "table_id": c.table_id,
         "page_number": c.page_number, "row": c.row, "col": c.col,
         "text": c.text, "numeric_value": c.numeric_value}
        for c in session.scalars(cell_stmt.limit(limit))]

    comm_stmt = scope(select(CommentaryRow).where(
        func.lower(CommentaryRow.text).like(like)), CommentaryRow)
    results["commentary"] = [
        {"id": c.id, "document_id": c.document_id, "page_number": c.page_number,
         "primary_topic": c.primary_topic, "text": c.text[:400]}
        for c in session.scalars(comm_stmt.limit(limit))]

    guid_stmt = scope(select(GuidanceRow).where(
        func.lower(GuidanceRow.text).like(like)), GuidanceRow)
    results["guidance"] = [
        {"id": g.id, "document_id": g.document_id, "page_number": g.page_number,
         "metric": g.metric, "text": g.text[:400], "nature": g.nature}
        for g in session.scalars(guid_stmt.limit(limit))]

    results_total = sum(len(v) for v in results.values())
    return {"query": q, "total": results_total, "results": results}


# ------------------------------------------------------------------ taxonomy
meta_router = APIRouter(tags=["metadata"])


@meta_router.get("/metrics")
def list_metrics() -> dict:
    return {"metrics": taxonomy.all_metrics(),
            "families": taxonomy.families()}
