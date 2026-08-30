"""Persistence for pipeline output.

Writes are idempotent per document: a reprocess replaces that document's
derived rows in one transaction rather than accumulating duplicates.  The
document row itself (and its review history) survives, so human corrections are
never destroyed by a re-run.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.ids import new_uuid, stable_id
from app.core.logging import get_logger
from app.database.models import (
    ChartRow, CommentaryRow, Document, FinancialFactRow, GuidanceRow, JobRow,
    OCRElementRow, Page, ProcessingRun, ProvenanceRow, RegionRow, ReviewActionRow,
    StageRun, TableCellRow, TableRow, ValidationResultRow,
)
from app.models.enums import DocumentStatus, StageStatus
from app.models.schemas import (
    Commentary, ExtractedChart, ExtractedTable, FinancialFact, Guidance,
    OCRElement, PageInfo, Region, StageRecord, ValidationResult,
)

log = get_logger("database.repository")

# Derived tables that a reprocess must clear before re-inserting.
DERIVED_MODELS = (
    ProvenanceRow, ValidationResultRow, GuidanceRow, CommentaryRow,
    FinancialFactRow, TableCellRow, TableRow, ChartRow, RegionRow,
    OCRElementRow,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _chunks(items: Sequence[Any], size: int = 900) -> Iterable[Sequence[Any]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


# ------------------------------------------------------------------ documents
def create_document(session: Session, *, document_id: str, filename: str,
                    original_filename: str, stored_path: str,
                    content_hash: str, size_bytes: int,
                    settings_snapshot: dict | None = None) -> Document:
    doc = Document(
        id=document_id, filename=filename, original_filename=original_filename,
        stored_path=stored_path, content_hash=content_hash,
        size_bytes=size_bytes, status=DocumentStatus.UPLOADED.value,
        settings_snapshot=settings_snapshot or {}, pdf_metadata={})
    session.add(doc)
    session.flush()
    return doc


def get_document(session: Session, document_id: str) -> Document | None:
    return session.get(Document, document_id)


def find_by_hash(session: Session, content_hash: str) -> Document | None:
    return session.scalars(
        select(Document).where(Document.content_hash == content_hash)
        .order_by(Document.created_at.desc()).limit(1)).first()


def list_documents(session: Session, *, limit: int = 50, offset: int = 0,
                   status: str | None = None, search: str | None = None
                   ) -> tuple[list[Document], int]:
    stmt = select(Document)
    count_stmt = select(func.count()).select_from(Document)
    if status:
        stmt = stmt.where(Document.status == status)
        count_stmt = count_stmt.where(Document.status == status)
    if search:
        like = f"%{search.lower()}%"
        cond = (func.lower(Document.original_filename).like(like)
                | func.lower(func.coalesce(Document.title, "")).like(like)
                | func.lower(func.coalesce(Document.company_name, "")).like(like))
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)
    total = session.scalar(count_stmt) or 0
    rows = list(session.scalars(
        stmt.order_by(Document.created_at.desc()).limit(limit).offset(offset)))
    return rows, total


def set_status(session: Session, document_id: str, status: DocumentStatus,
               *, error: str | None = None) -> None:
    doc = session.get(Document, document_id)
    if doc is None:
        return
    doc.status = status.value if hasattr(status, "value") else str(status)
    doc.error = error
    doc.updated_at = _utcnow()
    session.flush()


def delete_document(session: Session, document_id: str) -> bool:
    doc = session.get(Document, document_id)
    if doc is None:
        return False
    session.delete(doc)  # cascades to every child table
    session.flush()
    return True


# ----------------------------------------------------------------------- runs
def start_run(session: Session, document_id: str, *, trigger: str = "upload",
              settings_snapshot: dict | None = None,
              engine_versions: dict | None = None) -> ProcessingRun:
    run = ProcessingRun(
        id=new_uuid(), document_id=document_id, trigger=trigger,
        status="RUNNING", started_at=_utcnow(),
        settings_snapshot=settings_snapshot or {},
        engine_versions=engine_versions or {})
    session.add(run)
    session.flush()
    return run


def finish_run(session: Session, run_id: str, *, status: str,
               duration_ms: float | None = None,
               error: str | None = None) -> None:
    run = session.get(ProcessingRun, run_id)
    if run is None:
        return
    run.status = status
    run.finished_at = _utcnow()
    run.duration_ms = duration_ms
    run.error = error
    session.flush()


def upsert_stage(session: Session, run_id: str, document_id: str,
                 record: StageRecord) -> None:
    sid = stable_id("stage", run_id, record.stage)
    row = session.get(StageRun, sid)
    if row is None:
        row = StageRun(id=sid, run_id=run_id, document_id=document_id,
                       stage=record.stage)
        session.add(row)
    row.status = str(record.status)
    row.engine = record.engine
    row.duration_ms = record.duration_ms
    row.items_total = record.items_total
    row.items_done = record.items_done
    row.items_failed = record.items_failed
    row.error = record.error
    row.detail = record.detail or {}
    row.finished_at = record.finished_at or _utcnow()
    session.flush()


# --------------------------------------------------------------- derived data
def clear_derived(session: Session, document_id: str) -> None:
    """Remove everything a re-run regenerates.  Review history is preserved."""
    for model in DERIVED_MODELS:
        session.execute(delete(model).where(model.document_id == document_id))
    session.execute(delete(Page).where(Page.document_id == document_id))
    session.flush()


def save_pages(session: Session, pages: Sequence[PageInfo]) -> None:
    for p in pages:
        session.add(Page(
            id=p.id, document_id=p.document_id, page_number=p.page_number,
            pdf_width=p.pdf_width, pdf_height=p.pdf_height,
            render_width=p.render_width, render_height=p.render_height,
            dpi=p.dpi, rotation=p.rotation,
            has_text_layer=p.has_text_layer, text_layer_chars=p.text_layer_chars,
            is_scanned=p.is_scanned, ocr_engine=p.ocr_engine,
            ocr_signature=p.ocr_signature, ocr_element_count=p.ocr_element_count,
            ocr_mean_confidence=p.ocr_mean_confidence,
            ocr_duration_ms=p.ocr_duration_ms,
            region_count=p.region_count, table_count=p.table_count,
            chart_count=p.chart_count, section_title=p.section_title,
            entity_hint=p.entity_hint, status=str(p.status), error=p.error))
    session.flush()


def save_ocr_elements(session: Session, elements: Sequence[OCRElement]) -> None:
    rows = [OCRElementRow(
        id=e.id, document_id=e.document_id, page_number=e.page_number,
        text=e.text, confidence=e.confidence, x=e.bbox.x, y=e.bbox.y,
        width=e.bbox.width, height=e.bbox.height, polygon=e.polygon,
        block_id=e.block_id, line_id=e.line_id, word_id=e.word_id,
        source=str(e.source), reading_index=e.reading_index,
        font_size_px=e.font_size_px, is_bold=e.is_bold,
        corroborated=e.corroborated, aux_text=e.aux_text,
        currency_glyph=e.currency_glyph, region_id=e.block_id)
        for e in elements]
    for chunk in _chunks(rows):
        session.add_all(chunk)
        session.flush()


def save_regions(session: Session, regions: Sequence[Region]) -> None:
    rows = [RegionRow(
        id=r.id, document_id=r.document_id, page_number=r.page_number,
        type=str(r.type), x=r.bbox.x, y=r.bbox.y, width=r.bbox.width,
        height=r.bbox.height, confidence=r.confidence,
        reading_order=r.reading_order, column_index=r.column_index,
        text=r.text[:20000], parent_region_id=r.parent_region_id,
        detector=r.detector, meta=r.meta) for r in regions]
    for chunk in _chunks(rows):
        session.add_all(chunk)
        session.flush()


def save_tables(session: Session, tables: Sequence[ExtractedTable],
                entity_by_region: dict[str, str] | None = None) -> None:
    entity_by_region = entity_by_region or {}
    for t in tables:
        session.add(TableRow(
            id=t.id, document_id=t.document_id, page_number=t.page_number,
            region_id=t.region_id, title=t.title, caption=t.caption,
            x=t.bbox.x, y=t.bbox.y, width=t.bbox.width, height=t.bbox.height,
            n_rows=t.n_rows, n_cols=t.n_cols, detector=t.detector,
            confidence=t.confidence, has_ruling_lines=t.has_ruling_lines,
            unit_hint=str(t.unit_hint) if t.unit_hint else None,
            currency_hint=str(t.currency_hint) if t.currency_hint else None,
            entity_hint=entity_by_region.get(t.region_id or ""),
            columns=[c.model_dump() for c in t.columns],
            footnotes=t.footnotes, warnings=t.warnings,
            continued_from_table_id=t.continued_from_table_id))
        cells = [TableCellRow(
            id=c.id, table_id=c.table_id, document_id=c.document_id,
            page_number=c.page_number, row=c.row, col=c.col,
            row_span=c.row_span, col_span=c.col_span, text=c.text,
            role=str(c.role), x=c.bbox.x, y=c.bbox.y, width=c.bbox.width,
            height=c.bbox.height, confidence=c.confidence,
            numeric_value=c.numeric_value, raw_numeric_text=c.raw_numeric_text,
            is_numeric=c.is_numeric, element_ids=c.element_ids)
            for c in t.cells]
        for chunk in _chunks(cells):
            session.add_all(chunk)
        session.flush()


def save_charts(session: Session, charts: Sequence[ExtractedChart]) -> None:
    for c in charts:
        session.add(ChartRow(
            id=c.id, document_id=c.document_id, page_number=c.page_number,
            region_id=c.region_id, chart_type=str(c.chart_type),
            type_confidence=c.type_confidence, title=c.title,
            x=c.bbox.x, y=c.bbox.y, width=c.bbox.width, height=c.bbox.height,
            axis_labels=c.axis_labels, legend_labels=c.legend_labels,
            categories=c.categories,
            data_labels=[d.model_dump() for d in c.data_labels],
            unit_hint=str(c.unit_hint) if c.unit_hint else None,
            confidence=c.confidence, values_extracted=c.values_extracted,
            requires_review=c.requires_review, notes=c.notes))
    session.flush()


def save_facts(session: Session, facts: Sequence[FinancialFact]) -> None:
    rows: list[FinancialFactRow] = []
    prov_rows: list[ProvenanceRow] = []
    for f in facts:
        p = f.provenance
        np_ = f.period_normalized
        rows.append(FinancialFactRow(
            id=f.fact_id, document_id=f.document_id, metric=f.metric[:256],
            canonical_metric=f.canonical_metric,
            metric_display=f.metric_display[:256], entity=f.entity,
            kind=str(f.kind), nature=str(f.nature), value=f.value,
            raw_value=f.raw_value[:128], value_type=str(f.value_type),
            currency=str(f.currency), unit=str(f.unit), base_value=f.base_value,
            period=f.period, period_type=(str(np_.period_type) if np_ else None),
            fiscal_year=(np_.fiscal_year if np_ else None),
            quarter=(np_.quarter if np_ else None),
            period_raw=(np_.raw_text[:128] if np_ else None),
            comparison_period=f.comparison_period,
            comparison_value=f.comparison_value, growth=f.growth,
            growth_type=(str(f.growth_type) if f.growth_type else None),
            growth_reported=f.growth_reported, confidence=f.confidence,
            confidence_band=str(f.confidence_band),
            confidence_breakdown=(f.confidence_breakdown.model_dump()
                                  if f.confidence_breakdown else {}),
            validation_status=str(f.validation_status),
            requires_review=f.requires_review, review_state=str(f.review_state),
            source_page=f.source_page,
            region_id=(p.region_id if p else None),
            table_id=(p.table_id if p else None),
            cell_id=(p.cell_id if p else None),
            chart_id=(p.chart_id if p else None),
            original_text=f.original_text[:8000],
            context_text=f.context_text[:4000], meta=f.meta))
        if p is not None:
            prov_rows.append(ProvenanceRow(
                id=stable_id("prov", f.fact_id), document_id=f.document_id,
                fact_id=f.fact_id, target_type="fact", target_id=f.fact_id,
                page_number=p.page_number, region_id=p.region_id,
                table_id=p.table_id, cell_id=p.cell_id, chart_id=p.chart_id,
                x=(p.bbox.x if p.bbox else None), y=(p.bbox.y if p.bbox else None),
                width=(p.bbox.width if p.bbox else None),
                height=(p.bbox.height if p.bbox else None),
                original_text=p.original_text[:8000],
                ocr_element_ids=p.ocr_element_ids,
                extraction_method=p.extraction_method))
    for chunk in _chunks(rows):
        session.add_all(chunk)
        session.flush()
    for chunk in _chunks(prov_rows):
        session.add_all(chunk)
        session.flush()


def save_commentary(session: Session, items: Sequence[Commentary]) -> None:
    for c in items:
        session.add(CommentaryRow(
            id=c.id, document_id=c.document_id, page_number=c.page_number,
            region_id=c.region_id, text=c.text, topics=[str(t) for t in c.topics],
            primary_topic=str(c.primary_topic), sentiment=c.sentiment,
            speaker=c.speaker, is_quote=c.is_quote, confidence=c.confidence,
            x=(c.bbox.x if c.bbox else None), y=(c.bbox.y if c.bbox else None),
            width=(c.bbox.width if c.bbox else None),
            height=(c.bbox.height if c.bbox else None),
            summary=c.summary, summary_engine=c.summary_engine,
            mentioned_metrics=c.mentioned_metrics))
    session.flush()


def save_guidance(session: Session, items: Sequence[Guidance]) -> None:
    for g in items:
        session.add(GuidanceRow(
            id=g.id, document_id=g.document_id, page_number=g.page_number,
            region_id=g.region_id, text=g.text, nature=str(g.nature),
            canonical_metric=g.canonical_metric, metric=g.metric,
            target_value=g.target_value,
            target_unit=(str(g.target_unit) if g.target_unit else None),
            target_currency=(str(g.target_currency) if g.target_currency else None),
            target_period=g.target_period, direction=g.direction,
            trigger_phrase=g.trigger_phrase[:256], confidence=g.confidence,
            requires_review=g.requires_review,
            x=(g.bbox.x if g.bbox else None), y=(g.bbox.y if g.bbox else None),
            width=(g.bbox.width if g.bbox else None),
            height=(g.bbox.height if g.bbox else None)))
    session.flush()


def save_validations(session: Session, items: Sequence[ValidationResult]) -> None:
    seen: set[str] = set()
    rows = []
    for v in items:
        vid = v.id
        # results are content-addressed; a rule may legitimately fire twice with
        # identical inputs, so keep the first and drop exact repeats
        if vid in seen:
            continue
        seen.add(vid)
        rows.append(ValidationResultRow(
            id=vid, document_id=v.document_id, rule=v.rule,
            rule_family=v.rule_family, status=str(v.status),
            severity=str(v.severity), message=v.message,
            reported_value=v.reported_value, calculated_value=v.calculated_value,
            difference=v.difference, tolerance=v.tolerance,
            fact_ids=v.fact_ids, page_numbers=v.page_numbers, detail=v.detail))
    for chunk in _chunks(rows):
        session.add_all(chunk)
        session.flush()


def update_document_metadata(session: Session, document_id: str, *,
                             probe: Any = None, metadata: dict | None = None,
                             counts: dict | None = None,
                             processing_ms: float | None = None) -> None:
    doc = session.get(Document, document_id)
    if doc is None:
        return
    if probe is not None:
        doc.page_count = probe.page_count
        doc.pdf_version = probe.pdf_version
        doc.is_scanned = probe.is_scanned
        doc.text_layer_chars = probe.text_layer_chars
        doc.pdf_metadata = probe.metadata
    if metadata:
        doc.title = metadata.get("title")
        doc.company_name = metadata.get("company_name")
        doc.report_period = metadata.get("report_period")
        doc.report_type = metadata.get("report_type")
        doc.document_currency = (str(metadata["currency"])
                                 if metadata.get("currency") else None)
        doc.document_unit = (str(metadata["unit"])
                             if metadata.get("unit") else None)
    if counts:
        doc.pages_processed = counts.get("pages_processed", doc.pages_processed)
        doc.pages_failed = counts.get("pages_failed", doc.pages_failed)
    if processing_ms is not None:
        doc.processing_ms = processing_ms
        doc.processing_finished_at = _utcnow()
    doc.updated_at = _utcnow()
    session.flush()


# --------------------------------------------------------------------- counts
def document_counts(session: Session, document_id: str) -> dict[str, int]:
    def n(model, *conds) -> int:
        stmt = select(func.count()).select_from(model).where(
            model.document_id == document_id, *conds)
        return int(session.scalar(stmt) or 0)

    return {
        "fact_count": n(FinancialFactRow),
        "table_count": n(TableRow),
        "chart_count": n(ChartRow),
        "commentary_count": n(CommentaryRow),
        "guidance_count": n(GuidanceRow),
        "region_count": n(RegionRow),
        "ocr_element_count": n(OCRElementRow),
        "validation_warning_count": n(
            ValidationResultRow,
            ValidationResultRow.status.in_(["WARNING", "FAILED",
                                            "REQUIRES_REVIEW"])),
        "review_queue_count": n(
            FinancialFactRow, FinancialFactRow.requires_review.is_(True),
            FinancialFactRow.review_state == "pending"),
    }


# ---------------------------------------------------------------------- jobs
def enqueue_job(session: Session, document_id: str, *, job_type: str = "process",
                payload: dict | None = None, priority: int = 0) -> JobRow:
    job = JobRow(id=new_uuid(), document_id=document_id, job_type=job_type,
                 state="queued", priority=priority, payload=payload or {})
    session.add(job)
    session.flush()
    return job


def claim_job(session: Session, job_id: str) -> JobRow | None:
    job = session.get(JobRow, job_id)
    if job is None:
        return None
    job.state = "running"
    job.started_at = _utcnow()
    job.attempts += 1
    session.flush()
    return job


def finish_job(session: Session, job_id: str, *, state: str,
               error: str | None = None) -> None:
    job = session.get(JobRow, job_id)
    if job is None:
        return
    job.state = state
    job.finished_at = _utcnow()
    job.error = error
    session.flush()


def record_review(session: Session, *, document_id: str, target_type: str,
                  target_id: str, action: str, reviewer: str,
                  previous_value: dict | None, new_value: dict | None,
                  note: str | None) -> ReviewActionRow:
    row = ReviewActionRow(
        id=new_uuid(), document_id=document_id, target_type=target_type,
        target_id=target_id, action=action, reviewer=reviewer,
        previous_value=previous_value, new_value=new_value, note=note)
    session.add(row)
    session.flush()
    return row
